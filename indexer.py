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


DRIVE_CHECK_TIMEOUT_SEC = 5


def _path_exists_with_timeout(path: str, timeout_sec: float) -> bool:
    """os.path.existsを見切りをつけて呼ぶ。

    切断された共有フォルダにマップされたドライブレターは、存在確認だけで
    Windowsのネットワークタイムアウト(数十秒)に達することがある。
    daemonスレッド+join(timeout)で、応答のないドライブはスキップする。
    """
    result: dict = {}

    def _worker() -> None:
        result["exists"] = os.path.exists(path)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_sec)
    return result.get("exists", False)


def get_all_drives() -> list[str]:
    if platform.system() == "Windows":
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if _path_exists_with_timeout(drive, DRIVE_CHECK_TIMEOUT_SEC):
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


SCANDIR_TIMEOUT_SEC = 30


def _scandir_with_timeout(path: str, timeout_sec: float = SCANDIR_TIMEOUT_SEC) -> tuple[list, Optional[Exception]]:
    """os.scandirを見切りをつけて呼ぶ。

    切断されたネットワーク共有や応答しない外付けドライブ配下のフォルダは、
    列挙自体がOSレベルで長時間(数十秒〜)ブロックすることがある。全ドライブ
    スキャンではこうしたパスに当たり得るため、daemonスレッド+join(timeout)で
    見切りをつけ、1フォルダのハングでスキャン全体・キャンセル操作が止まる
    ことを防ぐ(コンテンツインデックスの抽出タイムアウトと同じ方式)。
    """
    result: dict = {}

    def _worker() -> None:
        try:
            result["entries"] = list(os.scandir(path))
        except OSError as e:
            result["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        return [], TimeoutError(f"{timeout_sec}秒以内に応答がありませんでした")
    if "error" in result:
        return [], result["error"]
    return result.get("entries", []), None


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
            # (パス, 深さ) で管理する。深さ0(root直下)だけタイムアウト保護付きの
            # scandirを使う。ネットワークドライブ・マップ済み共有が切断・応答なしに
            # なるリスクはroot(ドライブ文字/設定した対象フォルダそのもの)に集中する
            # ため、そこだけ保護すれば実用上のハングはほぼ防げる。すべての階層で
            # 保護すると、フォルダ1つごとにOSスレッドを生成することになり、
            # 大規模スキャン(数十万フォルダ)ではスレッド生成コストの積み重ねだけで
            # 顕著な遅延・失敗要因になり得るため、深い階層は素のscandirのままにする。
            stack: list[tuple[str, int]] = [(root, 0)]
            while stack:
                if _progress.cancel_requested:
                    break
                current, depth = stack.pop()
                _progress.current_folder = current
                if depth == 0:
                    entries, scandir_error = _scandir_with_timeout(current)
                    if scandir_error is not None:
                        _progress.error_count += 1
                        continue
                else:
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
                            stack.append((entry.path, depth + 1))
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
EXTRACTION_TIMEOUT_SEC = 60


def _extract_with_timeout(path: str, ext: str) -> tuple[str, Optional[str]]:
    """extract_textを別スレッドで実行し、一定時間で見切りをつける。

    壊れた/巨大なファイルの解析がフリーズすると、Pythonでは実行中のスレッドを
    安全に強制終了できないため、daemonスレッド+join(timeout)で「諦めて次の
    ファイルへ進む」形にする。タイムアウトしたスレッド自体は残り続けるが、
    daemon指定によりアプリの終了は妨げない。これによりキャンセルボタンも
    最大でもこの秒数以内には反映されるようになる。
    """
    result: dict = {}

    def _worker() -> None:
        try:
            result["text"] = extractor.extract_text(path, ext) or ""
        except extractor.ExtractionError as e:
            result["error"] = str(e)
        except (PermissionError, OSError) as e:
            result["error"] = f"読み込みエラー: {e}"
        except Exception as e:
            result["error"] = f"予期しないエラー: {e}"

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(EXTRACTION_TIMEOUT_SEC)
    if t.is_alive():
        return "", f"抽出が{EXTRACTION_TIMEOUT_SEC}秒以内に完了しなかったためスキップしました"
    if "error" in result:
        return "", result["error"]
    return result.get("text", ""), None


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
    extract_error_count: int = 0
    embed_error_count: int = 0
    embedded_count: int = 0
    cancel_requested: bool = False
    embedder_available: bool = True
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error_message: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "phase": self.phase,
            "current_file": self.current_file,
            "processed_count": self.processed_count,
            "total_count": self.total_count,
            "error_count": self.error_count,
            "extract_error_count": self.extract_error_count,
            "embed_error_count": self.embed_error_count,
            "embedded_count": self.embedded_count,
            "embedder_available": self.embedder_available,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_message": self.error_message,
            "last_error": self.last_error,
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
    error_log_path: Optional[Path] = None,
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
        _content_progress.extract_error_count = 0
        _content_progress.embed_error_count = 0
        _content_progress.embedded_count = 0
        _content_progress.cancel_requested = False
        _content_progress.embedder_available = True
        _content_progress.started_at = time.time()
        _content_progress.finished_at = None
        _content_progress.error_message = None
        _content_progress.last_error = None

    global _content_thread

    def _target() -> None:
        try:
            _run_content_index(db_path, models_root, error_log_path)
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


def _log_content_error(error_log_path: Optional[Path], kind: str, path: str, message: str) -> None:
    """エラーの原因調査用に、ファイル単位のエラーをローカルログへ追記する。

    仕様の「例外を握りつぶさない」方針に沿い、UIのカウントだけでなく中身を残す。
    ログ書き込み自体の失敗でインデックス処理を止めない。
    """
    _content_progress.last_error = f"{message}({path})"
    if error_log_path is None:
        return
    try:
        import json

        error_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(error_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"timestamp": time.time(), "kind": kind, "path": path, "error": message},
                ensure_ascii=False,
            ) + "\n")
    except OSError:
        pass


def _run_content_index(db_path: Path, models_root: Path, error_log_path: Optional[Path] = None) -> None:
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
    except Exception as e:
        # 埋め込みが使えなくても全文抽出+ファイル名検索の強化(FTS)自体は続行する(縮退運転)。
        # onnxruntime/tokenizersが投げうる例外はEmbedderError以外もあり得るため広く捕捉する。
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
        # 未抽出/更新されたファイルに加え、埋め込みモデルが今回利用可能なら、
        # 過去に縮退運転(モデル未利用)で抽出だけ済ませたファイルも埋め込み対象に含める。
        condition = "(fc.file_id IS NULL OR fc.extracted_at < f.mtime)"
        if embedder_instance is not None:
            condition = f"({condition} OR (fc.error IS NULL AND fc.embedded_at IS NULL))"
        rows = conn.execute(
            f"""
            SELECT f.id, f.path, f.ext, f.mtime FROM files f
            LEFT JOIN file_content fc ON fc.file_id = f.id
            WHERE f.is_deleted = 0 AND f.ext IN ({placeholders}) AND {condition}
            """,
            list(extractable),
        ).fetchall()
        _content_progress.total_count = len(rows)

        for row in rows:
            if _content_progress.cancel_requested:
                break
            _content_progress.current_file = row["path"]

            # 既に本文抽出済み(縮退運転で埋め込みだけ未処理)で、かつファイルが更新されて
            # いない場合に限り再抽出をスキップし、保存済みのテキストを使って埋め込みだけ行う。
            # mtimeが進んでいる場合は内容が変わっている可能性があるため必ず再抽出する。
            existing = conn.execute(
                "SELECT text, error, extracted_at FROM file_content WHERE file_id=?", (row["id"],)
            ).fetchone()
            needs_reextract = (
                existing is None
                or existing["error"] is not None
                or existing["extracted_at"] < row["mtime"]
            )
            if needs_reextract:
                text, error = _extract_with_timeout(row["path"], row["ext"])
            else:
                text, error = existing["text"], None

            # 埋め込みの計算はDBへの書き込みを始める前に済ませる。書き込み開始後に
            # 重い推論を挟むと、その間ずっとSQLiteの書き込みロックを保持してしまい、
            # 並行する差分スキャンが「database is locked」で失敗し得るため。
            now = time.time()
            embedded_at = None
            chunk_rows: Optional[list[tuple]] = None
            if error:
                _content_progress.error_count += 1
                _content_progress.extract_error_count += 1
                _log_content_error(error_log_path, "extract", row["path"], error)
            elif embedder_instance is not None:
                embedded_at = now  # 空文書などでチャンク0件でも「試みた」ことにして無限リトライを避ける
                if text.strip():
                    chunks = _chunk_text(text)
                    try:
                        vecs = embedder_instance.embed_passages(chunks)
                        chunk_rows = [
                            (row["id"], i, chunk, embedder.pack_vector(vecs[i]))
                            for i, chunk in enumerate(chunks)
                        ]
                    except Exception as e:
                        _content_progress.error_count += 1
                        _content_progress.embed_error_count += 1
                        _log_content_error(error_log_path, "embed", row["path"], str(e))
                        embedded_at = None  # 失敗時は次回また埋め込みを試みる

            # ここからDB書き込み。1ファイル分をまとめて短時間で書き、都度コミットして
            # ロック保持時間を最小化する(WAL+synchronous=NORMALでは都度コミットは安価)。
            if needs_reextract:
                conn.execute(
                    """
                    INSERT INTO file_content(file_id, text, char_count, extracted_at, extractor_version, error, embedded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        text=excluded.text, char_count=excluded.char_count,
                        extracted_at=excluded.extracted_at,
                        extractor_version=excluded.extractor_version, error=excluded.error,
                        embedded_at=excluded.embedded_at
                    """,
                    (row["id"], text, len(text), now, extractor.EXTRACTOR_VERSION, error, embedded_at),
                )
                # テキストが変わった(可能性がある)ので古いチャンクは必ず破棄する
                conn.execute("DELETE FROM file_chunks WHERE file_id=?", (row["id"],))
            else:
                # 埋め込みだけの追加処理ではtextをSET句に含めない。含めると
                # file_content_auトリガーが発火し、内容が同じでも本文FTS索引の
                # 削除・再構築が全対象ファイル分走ってしまう(Phase0で修正した
                # ファイル名FTSと同種の性能問題)。
                conn.execute(
                    "UPDATE file_content SET embedded_at=? WHERE file_id=?",
                    (embedded_at, row["id"]),
                )
            if chunk_rows is not None:
                if not needs_reextract:
                    conn.execute("DELETE FROM file_chunks WHERE file_id=?", (row["id"],))
                conn.executemany(
                    "INSERT INTO file_chunks(file_id, chunk_index, chunk_text, embedding) "
                    "VALUES (?, ?, ?, ?)",
                    chunk_rows,
                )
                _content_progress.embedded_count += 1
            conn.commit()

            _content_progress.processed_count += 1
    finally:
        conn.close()
