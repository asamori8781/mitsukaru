"""FTS5(trigram)を用いたローカルファイル検索。"""
from __future__ import annotations

import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

# trigramトークナイザは3文字未満の語句を検索できないため、
# 短いキーワードはLIKEによる部分一致にフォールバックする。
FTS_MIN_LEN = 3
DEFAULT_LIMIT = 50

_HIRA_TO_KATA = str.maketrans({chr(cp): chr(cp + 0x60) for cp in range(0x3041, 0x3097)})
_KATA_TO_HIRA = str.maketrans({chr(cp): chr(cp - 0x60) for cp in range(0x30A1, 0x30F7)})
_ASCII_TO_FULLWIDTH = str.maketrans({chr(cp): chr(cp + 0xFEE0) for cp in range(0x21, 0x7F)})


def _keyword_variants(keyword: str) -> list[str]:
    """表記ゆれを吸収するための照合バリアントを生成する。

    trigram索引はひらがな/カタカナ、全角/半角を同一視しないため、
    ひらがな⇔カタカナ変換・NFKC正規化(全角英数→半角等)・半角→全角変換を
    検索側で展開してOR照合する。
    """
    variants: dict[str, None] = {keyword: None}
    variants[unicodedata.normalize("NFKC", keyword)] = None
    for base in list(variants):
        variants[base.translate(_ASCII_TO_FULLWIDTH)] = None
    for base in list(variants):
        variants[base.translate(_HIRA_TO_KATA)] = None
        variants[base.translate(_KATA_TO_HIRA)] = None
    return list(variants)


@dataclass
class SearchResult:
    id: int
    name: str
    path: str
    dir: str
    ext: str
    size: int
    mtime: float
    matched_keywords: list[str]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_fts_phrase(value: str) -> str:
    return value.replace('"', '""')


def _match_single(conn: sqlite3.Connection, keyword: str) -> set[int]:
    if len(keyword) < FTS_MIN_LEN:
        like = f"%{_escape_like(keyword)}%"
        rows = conn.execute(
            "SELECT id FROM files WHERE is_deleted=0 AND "
            "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')",
            (like, like),
        ).fetchall()
        return {row[0] for row in rows}
    phrase = '"' + _escape_fts_phrase(keyword) + '"'
    rows = conn.execute(
        """
        SELECT f.id FROM files_fts
        JOIN files f ON f.id = files_fts.rowid
        WHERE files_fts MATCH ? AND f.is_deleted = 0
        """,
        (phrase,),
    ).fetchall()
    return {row[0] for row in rows}


def _match_keyword(conn: sqlite3.Connection, keyword: str) -> set[int]:
    keyword = keyword.strip()
    if not keyword:
        return set()
    ids: set[int] = set()
    for variant in _keyword_variants(keyword):
        ids |= _match_single(conn, variant)
    return ids


def _fetch_rows(
    conn: sqlite3.Connection,
    match_map: dict[int, set[str]],
    extensions: Optional[list[str]],
    recency_days: Optional[int],
) -> list[SearchResult]:
    ids = list(match_map.keys())
    placeholders = ",".join("?" * len(ids))
    query = (
        f"SELECT id, name, path, dir, ext, size, mtime FROM files "
        f"WHERE id IN ({placeholders}) AND is_deleted=0"
    )
    params: list = list(ids)
    if extensions:
        ext_placeholders = ",".join("?" * len(extensions))
        query += f" AND ext IN ({ext_placeholders})"
        params.extend(e.lower() for e in extensions)
    if recency_days is not None:
        cutoff = time.time() - recency_days * 86400
        query += " AND mtime >= ?"
        params.append(cutoff)
    rows = conn.execute(query, params).fetchall()
    return [
        SearchResult(
            id=row["id"], name=row["name"], path=row["path"], dir=row["dir"],
            ext=row["ext"], size=row["size"], mtime=row["mtime"],
            matched_keywords=sorted(match_map[row["id"]]),
        )
        for row in rows
    ]


def search_files(
    conn: sqlite3.Connection,
    keywords: list[str],
    extensions: Optional[list[str]] = None,
    recency_days: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchResult]:
    """展開キーワードでOR検索し、一致キーワード種類数→更新日時の新しさの順で返す。

    extensions/recency_daysを指定して0件になる場合は、絞り込みを外して再検索する。
    """
    match_map: dict[int, set[str]] = {}
    for keyword in keywords:
        for file_id in _match_keyword(conn, keyword):
            match_map.setdefault(file_id, set()).add(keyword)

    if not match_map:
        return []

    results = _fetch_rows(conn, match_map, extensions, recency_days)
    if not results and (extensions or recency_days is not None):
        results = _fetch_rows(conn, match_map, None, None)

    results.sort(key=lambda r: (-len(r.matched_keywords), -r.mtime))
    return results[:limit]
