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

CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
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
    return conn


def init_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
