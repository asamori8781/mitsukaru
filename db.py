"""SQLite接続とスキーマ(files / files_fts)の初期化、バージョンチェック。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIN_SQLITE_VERSION = (3, 34, 0)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    ext          TEXT NOT NULL,
    dir          TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    is_deleted   INTEGER NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_is_deleted ON files(is_deleted);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name,
    path,
    content='files',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;

CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
END;

-- 差分スキャンはsize/mtime/last_seen_at等しか更新しないため、name/pathがSET句に
-- 含まれる時だけFTSを組み直す。無条件のAFTER UPDATEにすると差分スキャンのたびに
-- 全ファイル分のtrigramインデックスが削除・再構築され、検索が数分単位で固まる。
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF name, path ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
    INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;

-- Phase 1: 抽出した本文テキスト(files 1:1)。extracted_atはfiles.mtimeと比較して
-- 再抽出が必要かどうかを判定するための基準時刻(抽出失敗時もこの行自体は作成し、
-- errorに理由を記録することで、失敗ファイルへの再試行を毎回繰り返さない)。
-- embedded_atは埋め込みモデルが利用可能な状態で埋め込み処理を試みた時刻。
-- 埋め込みモデル未ダウンロードのまま抽出だけ行った(縮退運転)場合はNULLのまま
-- とし、後でモデルが使えるようになった際に埋め込みだけ再試行できるようにする。
CREATE TABLE IF NOT EXISTS file_content (
    file_id           INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    text              TEXT NOT NULL,
    char_count        INTEGER NOT NULL,
    extracted_at      REAL NOT NULL,
    extractor_version INTEGER NOT NULL,
    error             TEXT,
    embedded_at       REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS file_content_fts USING fts5(
    text, content='file_content', content_rowid='file_id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS file_content_ai AFTER INSERT ON file_content BEGIN
    INSERT INTO file_content_fts(rowid, text) VALUES (new.file_id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS file_content_ad AFTER DELETE ON file_content BEGIN
    INSERT INTO file_content_fts(file_content_fts, rowid, text) VALUES('delete', old.file_id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS file_content_au AFTER UPDATE OF text ON file_content BEGIN
    INSERT INTO file_content_fts(file_content_fts, rowid, text) VALUES('delete', old.file_id, old.text);
    INSERT INTO file_content_fts(rowid, text) VALUES (new.file_id, new.text);
END;

-- files削除(論理削除ではなく物理削除)時にfile_contentも連動して消えるよう、
-- 外部キーのON DELETE CASCADEを利用する(get_connectionでPRAGMA foreign_keys=ONが必要)。

-- Phase 1: チャンク分割した本文と埋め込みベクトル(files 1:N)。
-- embeddingはfloat32配列をnumpy.tobytes()でパックしたBLOB。
CREATE TABLE IF NOT EXISTS file_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    UNIQUE(file_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_file_chunks_file_id ON file_chunks(file_id);
"""

# 旧バージョンのDB(無条件AFTER UPDATEトリガー)を新定義へ移行するためのSQL。
# CREATE TRIGGER IF NOT EXISTSでは既存定義が置き換わらないため、明示的に作り直す。
MIGRATE_SQL = """
DROP TRIGGER IF EXISTS files_au;
CREATE TRIGGER files_au AFTER UPDATE OF name, path ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
    INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;
"""


class UnsupportedSQLiteError(Exception):
    """sqlite3のバージョンがFTS5 trigramトークナイザの要件を満たさない場合のエラー。"""


def check_sqlite_version() -> None:
    version = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    if version < MIN_SQLITE_VERSION:
        raise UnsupportedSQLiteError(
            "SQLiteのバージョンが古いため起動できません"
            f"(検出: {sqlite3.sqlite_version} / 必要: 3.34.0以上)。"
            "Pythonの実行環境を更新してください。"
        )


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    # SQLiteに「ALTER TABLE ADD COLUMN IF NOT EXISTS」はないため存在確認してから追加する。
    if not _column_exists(conn, "file_content", "embedded_at"):
        conn.execute("ALTER TABLE file_content ADD COLUMN embedded_at REAL")


def init_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(MIGRATE_SQL)
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()
