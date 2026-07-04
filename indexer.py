"""ファイルメタデータのスキャン(全体/差分)、進捗管理、Phase1予測サイズ算出。

ファイルの中身は一切読まない(Phase 0のスコープ)。名前・パス・拡張子・
サイズ・更新日時のみをDBへ登録する。
"""
from __future__ import annotations

import os
import platform
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import db
import embedder
import extractor

BATCH_SIZE = 2000

# Phase1(全文インデックス)の予測サイズ算出用の概算係数。
# テキスト系はファイルサイズそのものをテキスト量とみなし、
# docx/xlsx/pptx・pdfはテキスト抽出できる割合を概算で見積もる。
TEXT_LIKE_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".ini",
    ".yaml", ".yml", ".py", ".js", ".ts", ".css", ".java", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".go", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".rtf",
}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
OFFICE_TEXT_RATIO = 0.06
PDF_TEXT_RATIO = 0.10
PHASE1_INDEX_MULTIPLIER = 2.5


def get_all_drives() -> list[str]:
    if platform.system() == "Windows":
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        return drives
    # Windows以外(開発機での動作確認用)はルートを対象にする
    return ["/"]


def resolve_scan_roots(scan_all_drives: bool, target_folders: list[str]) -> list[str]:
    if scan_all_drives:
        return get_all_drives()
    return [f for f in target_folders if os.path.isdir(f)]


@dataclass
class ScanProgress:
    running: bool = False
    mode: str = ""
    current_folder: str = ""
    processed_count: int = 0
    error_count: int = 0
    cancel_requested: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    summary: Optional[dict] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "current_folder": self.current_folder,
            "processed_count": self.processed_count,
            "error_count": self.error_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "error_message": self.error_message,
        }


_progress = ScanProgress()
_start_lock = threading.Lock()
_scan_thread: Optional[threading.Thread] = None


def get_progress() -> dict:
    return _progress.to_dict()


def is_running() -> bool:
    return _progress.running


def cancel_scan() -> None:
    _progress.cancel_requested = True


def start_scan(
    mode: str,
    roots: list[str],
    exclude_folders: list[str],
    exclude_extensions: list[str],
    db_path: Path,
    on_finish: Optional[Callable[[dict], None]] = None,
) -> bool:
    """バックグラウンドスレッドでスキャンを開始する。既に実行中ならFalseを返す。"""
    global _scan_thread
    with _start_lock:
        if _progress.running:
            return False
        _progress.running = True
        _progress.mode = mode
        _progress.current_folder = ""
        _progress.processed_count = 0
        _progress.error_count = 0
        _progress.cancel_requested = False
        _progress.started_at = time.time()
        _progress.finished_at = None
        _progress.summary = None
        _progress.error_message = None

    exclude_folders_lower = {f.lower() for f in exclude_folders}
    exclude_extensions_lower = {e.lower() for e in exclude_extensions}

    def _target() -> None:
        try:
            _run_scan(roots, exclude_folders_lower, exclude_extensions_lower, db_path)
        except Exception as e:  # スキャン中の予期しない例外もUIへ伝える
            _progress.error_message = f"スキャン中に予期しないエラーが発生しました: {e}"
        finally:
            summary = None
            try:
                summary = compute_summary(db_path)
            except Exception:
                pass
            _progress.running = False
            _progress.finished_at = time.time()
            _progress.summary = summary
            if on_finish:
                on_finish(summary or {})

    _scan_thread = threading.Thread(target=_target, daemon=True)
    _scan_thread.start()
    return True


def _is_hidden(entry: "os.DirEntry[str]") -> bool:
    if entry.name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
        except (AttributeError, OSError):
            return False
    return False


def _is_traversal_unsafe_dir(entry: "os.DirEntry[str]") -> bool:
    """ディレクトリとして辿ると無限ループの恐れがあるもの(シンボリックリンク/
    ジャンクション等のリパースポイント)を判定する。ディレクトリ専用。

    ファイルには適用しないこと。OneDriveのファイルオンデマンドでは通常のファイルも
    リパースポイント属性を持つため、ファイルまで除外するとOneDrive配下(ドキュメント
    フォルダのリダイレクト先など)が丸ごとインデックスから漏れる。
    """
    try:
        if entry.is_symlink():
            return True
        if os.name == "nt":
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        # 属性取得に失敗した場合は安全側に倒して辿らない
        return True
    return False


def _is_symlink_file(entry: "os.DirEntry[str]") -> bool:
    try:
        return entry.is_symlink()
    except OSError:
        return True


def _flush_batch(conn, batch: list[tuple]) -> None:
    # name/ext/dirはpathから導出される値であり、path衝突時に変わることはないため
    # SET句に含めない。SET句に列挙するだけでfiles_auトリガー(UPDATE OF name, path)が
    # 発火し、差分スキャンのたびに全件のFTS再構築が走ってしまう。
    conn.executemany(
        """
        INSERT INTO files (path, name, ext, dir, size, mtime, is_deleted, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(path) DO UPDATE SET
            size=excluded.size, mtime=excluded.mtime,
            is_deleted=0, last_seen_at=excluded.last_seen_at
        """,
        batch,
    )
    conn.commit()


def _run_scan(
    roots: list[str],
    exclude_folders: set[str],
    exclude_extensions: set[str],
    db_path: Path,
) -> None:
    scan_started_at = time.time()
    conn = db.get_connection(db_path)
    try:
        batch: list[tuple] = []
        for root in roots:
            if _progress.cancel_requested:
                break
            stack = [root]
            while stack:
                if _progress.cancel_requested:
                    break
                current = stack.pop()
                _progress.current_folder = current
                try:
                    entries = list(os.scandir(current))
                except (PermissionError, OSError):
                    _progress.error_count += 1
                    continue
                for entry in entries:
                    if _progress.cancel_requested:
                        break
                    try:
                        if _is_hidden(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in exclude_folders:
                                continue
                            if _is_traversal_unsafe_dir(entry):
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if _is_symlink_file(entry):
                                continue
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in exclude_extensions:
                                continue
                            st = entry.stat(follow_symlinks=False)
                            batch.append((
                                entry.path, entry.name, ext, os.path.dirname(entry.path),
                                st.st_size, st.st_mtime, scan_started_at,
                            ))
                            _progress.processed_count += 1
                            if len(batch) >= BATCH_SIZE:
                                _flush_batch(conn, batch)
                                batch = []
                    except (PermissionError, OSError):
                        _progress.error_count += 1
                        continue
        if batch:
            _flush_batch(conn, batch)

        if not _progress.cancel_requested:
            for root in roots:
                conn.execute(
                    "UPDATE files SET is_deleted=1 "
                    "WHERE is_deleted=0 AND last_seen_at < ? AND substr(path,1,length(?))=?",
                    (scan_started_at, root, root),
                )
            conn.commit()
    finally:
        conn.close()


def _phase1_ratio(ext: str) -> float:
    if ext in TEXT_LIKE_EXTENSIONS:
        return 1.0
    if ext in OFFICE_EXTENSIONS:
        return OFFICE_TEXT_RATIO
    if ext == ".pdf":
        return PDF_TEXT_RATIO
    return 0.0


def compute_summary(db_path: Path) -> dict:
    conn = db.get_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM files WHERE is_deleted=0").fetchone()[0]
        rows = conn.execute(
            "SELECT ext, SUM(size) FROM files WHERE is_deleted=0 GROUP BY ext"
        ).fetchall()
    finally:
        conn.close()
    text_bytes = sum((row[1] or 0) * _phase1_ratio(row[0]) for row in rows)
    phase1_estimate_bytes = int(text_bytes * PHASE1_INDEX_MULTIPLIER)
    db_size_bytes = db_path.stat().st_size if db_path.exists() else 0
    return {
        "file_count": total,
        "db_size_bytes": db_size_bytes,
        "phase1_estimate_bytes": phase1_estimate_bytes,
    }


# ---- Phase 1: コンテンツインデックス作成(全文抽出+埋め込み) ----

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
CONTENT_COMMIT_EVERY = 50


def _chunk_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


@dataclass
class ContentIndexProgress:
    running: bool = False
    phase: str = ""  # "downloading_model" | "indexing"
    current_file: str = ""
    processed_count: int = 0
    total_count: int = 0
    error_count: int = 0
    embedded_count: int = 0
    cancel_requested: bool = False
    embedder_available: bool = True
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "phase": self.phase,
            "current_file": self.current_file,
            "processed_count": self.processed_count,
            "total_count": self.total_count,
            "error_count": self.error_count,
            "embedded_count": self.embedded_count,
            "embedder_available": self.embedder_available,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_message": self.error_message,
        }


_content_progress = ContentIndexProgress()
_content_start_lock = threading.Lock()
_content_thread: Optional[threading.Thread] = None


def get_content_progress() -> dict:
    return _content_progress.to_dict()


def is_content_indexing() -> bool:
    return _content_progress.running


def cancel_content_index() -> None:
    _content_progress.cancel_requested = True


def start_content_index(
    db_path: Path,
    models_root: Path,
    on_finish: Optional[Callable[[], None]] = None,
) -> bool:
    with _content_start_lock:
        if _content_progress.running:
            return False
        _content_progress.running = True
        _content_progress.phase = "downloading_model" if not embedder.is_downloaded(models_root) else "indexing"
        _content_progress.current_file = ""
        _content_progress.processed_count = 0
        _content_progress.total_count = 0
        _content_progress.error_count = 0
        _content_progress.embedded_count = 0
        _content_progress.cancel_requested = False
        _content_progress.embedder_available = True
        _content_progress.started_at = time.time()
        _content_progress.finished_at = None
        _content_progress.error_message = None

    global _content_thread

    def _target() -> None:
        try:
            _run_content_index(db_path, models_root)
        except Exception as e:  # 予期しない例外もUIへ伝える
            _content_progress.error_message = f"コンテンツインデックス作成中に予期しないエラーが発生しました: {e}"
        finally:
            _content_progress.running = False
            _content_progress.finished_at = time.time()
            if on_finish:
                on_finish()

    _content_thread = threading.Thread(target=_target, daemon=True)
    _content_thread.start()
    return True


def _run_content_index(db_path: Path, models_root: Path) -> None:
    embedder_instance: Optional[embedder.Embedder] = None
    try:
        if not embedder.is_downloaded(models_root):
            _content_progress.phase = "downloading_model"

            def _on_dl_progress(name: str, downloaded: int, total: int) -> None:
                _content_progress.current_file = f"モデルをダウンロード中: {name}"
                _content_progress.processed_count = downloaded
                _content_progress.total_count = total

            embedder.download_model(models_root, on_progress=_on_dl_progress)
        embedder_instance = embedder.Embedder(models_root)
    except embedder.EmbedderError as e:
        # 埋め込みが使えなくても全文抽出+ファイル名検索の強化(FTS)自体は続行する(縮退運転)
        _content_progress.embedder_available = False
        _content_progress.error_message = (
            f"埋め込みモデルを利用できないため、全文検索のみ有効にします({e})"
        )

    _content_progress.phase = "indexing"
    _content_progress.current_file = ""
    _content_progress.processed_count = 0
    _content_progress.total_count = 0

    conn = db.get_connection(db_path)
    try:
        extractable = extractor.extractable_extensions()
        placeholders = ",".join("?" * len(extractable))
        rows = conn.execute(
            f"""
            SELECT f.id, f.path, f.ext, f.mtime FROM files f
            LEFT JOIN file_content fc ON fc.file_id = f.id
            WHERE f.is_deleted = 0 AND f.ext IN ({placeholders})
              AND (fc.file_id IS NULL OR fc.extracted_at < f.mtime)
            """,
            list(extractable),
        ).fetchall()
        _content_progress.total_count = len(rows)

        since_commit = 0
        for row in rows:
            if _content_progress.cancel_requested:
                break
            _content_progress.current_file = row["path"]

            error: Optional[str] = None
            text = ""
            try:
                text = extractor.extract_text(row["path"], row["ext"]) or ""
            except extractor.ExtractionError as e:
                error = str(e)
            except (PermissionError, OSError) as e:
                error = f"読み込みエラー: {e}"

            now = time.time()
            conn.execute(
                """
                INSERT INTO file_content(file_id, text, char_count, extracted_at, extractor_version, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    text=excluded.text, char_count=excluded.char_count,
                    extracted_at=excluded.extracted_at,
                    extractor_version=excluded.extractor_version, error=excluded.error
                """,
                (row["id"], text, len(text), now, extractor.EXTRACTOR_VERSION, error),
            )
            conn.execute("DELETE FROM file_chunks WHERE file_id=?", (row["id"],))

            if error:
                _content_progress.error_count += 1
            elif embedder_instance is not None and text.strip():
                chunks = _chunk_text(text)
                try:
                    vecs = embedder_instance.embed_passages(chunks)
                    chunk_rows = [
                        (row["id"], i, chunk, embedder.pack_vector(vecs[i]))
                        for i, chunk in enumerate(chunks)
                    ]
                    conn.executemany(
                        "INSERT INTO file_chunks(file_id, chunk_index, chunk_text, embedding) "
                        "VALUES (?, ?, ?, ?)",
                        chunk_rows,
                    )
                    _content_progress.embedded_count += 1
                except Exception:
                    _content_progress.error_count += 1

            _content_progress.processed_count += 1
            since_commit += 1
            if since_commit >= CONTENT_COMMIT_EVERY:
                conn.commit()
                since_commit = 0
        conn.commit()
    finally:
        conn.close()
