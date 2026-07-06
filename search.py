"""FTS5(trigram)を用いたローカルファイル検索(ファイル名+本文+意味検索)。"""
from __future__ import annotations

import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

import embedder

# trigramトークナイザは3文字未満の語句を検索できないため、
# 短いキーワードはLIKEによる部分一致にフォールバックする。
FTS_MIN_LEN = 3
DEFAULT_LIMIT = 50
SNIPPET_LEN = 120

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
    matched_keywords: list[str] = field(default_factory=list)
    matched_in_name: bool = False
    matched_in_content: bool = False
    semantic_score: Optional[float] = None
    snippet: Optional[str] = None


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


def _match_content_single(conn: sqlite3.Connection, keyword: str) -> set[int]:
    if len(keyword) < FTS_MIN_LEN:
        like = f"%{_escape_like(keyword)}%"
        rows = conn.execute(
            "SELECT fc.file_id FROM file_content fc JOIN files f ON f.id = fc.file_id "
            "WHERE f.is_deleted=0 AND fc.text LIKE ? ESCAPE '\\'",
            (like,),
        ).fetchall()
        return {row[0] for row in rows}
    phrase = '"' + _escape_fts_phrase(keyword) + '"'
    rows = conn.execute(
        """
        SELECT fc.file_id FROM file_content_fts
        JOIN file_content fc ON fc.file_id = file_content_fts.rowid
        JOIN files f ON f.id = fc.file_id
        WHERE file_content_fts MATCH ? AND f.is_deleted = 0
        """,
        (phrase,),
    ).fetchall()
    return {row[0] for row in rows}


def _match_content_keyword(conn: sqlite3.Connection, keyword: str) -> set[int]:
    keyword = keyword.strip()
    if not keyword:
        return set()
    ids: set[int] = set()
    for variant in _keyword_variants(keyword):
        ids |= _match_content_single(conn, variant)
    return ids


def _fetch_rows(
    conn: sqlite3.Connection,
    ids: list[int],
    extensions: Optional[list[str]],
    recency_days: Optional[int],
) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
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
    return {row["id"]: row for row in rows}


def search_files(
    conn: sqlite3.Connection,
    keywords: list[str],
    extensions: Optional[list[str]] = None,
    recency_days: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchResult]:
    """ファイル名のみを対象にOR検索する(キーワード編集後のローカル再検索用。APIは呼ばない)。

    一致キーワード種類数→更新日時の新しさの順で返す。extensions/recency_daysを
    指定して0件になる場合は、絞り込みを外して再検索する。
    """
    match_map: dict[int, set[str]] = {}
    for keyword in keywords:
        for file_id in _match_keyword(conn, keyword):
            match_map.setdefault(file_id, set()).add(keyword)

    if not match_map:
        return []

    rows_by_id = _fetch_rows(conn, list(match_map.keys()), extensions, recency_days)
    if not rows_by_id and (extensions or recency_days is not None):
        rows_by_id = _fetch_rows(conn, list(match_map.keys()), None, None)

    results = [
        SearchResult(
            id=row["id"], name=row["name"], path=row["path"], dir=row["dir"],
            ext=row["ext"], size=row["size"], mtime=row["mtime"],
            matched_keywords=sorted(match_map[row["id"]]), matched_in_name=True,
        )
        for row in rows_by_id.values()
    ]
    results.sort(key=lambda r: (-len(r.matched_keywords), -r.mtime))
    return results[:limit]


def _make_snippet(text: str, around: int = SNIPPET_LEN) -> str:
    snippet = text.strip().replace("\n", " ")
    if len(snippet) <= around * 2:
        return snippet
    return snippet[: around * 2] + "…"


SEMANTIC_SCAN_BATCH = 5000


def semantic_search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    exclude_ids: set[int],
    limit: int,
    min_score: float,
) -> list[tuple[int, float, str]]:
    """埋め込み類似度でファイルを検索する(キーワードで既にヒットしたファイルは除外)。

    数十万チャンク規模でもメモリを圧迫しないよう、埋め込みはバッチ単位で
    読み出してスコア計算し、チャンク本文は上位ヒット分だけ後から取得する。
    戻り値は類似度降順の (file_id, スコア, 最も類似したチャンクの抜粋)。
    """
    cursor = conn.execute(
        "SELECT fch.id, fch.file_id, fch.embedding FROM file_chunks fch "
        "JOIN files f ON f.id = fch.file_id WHERE f.is_deleted = 0"
    )
    best: dict[int, tuple[float, int]] = {}  # file_id -> (score, chunk_id)
    while True:
        rows = cursor.fetchmany(SEMANTIC_SCAN_BATCH)
        if not rows:
            break
        vecs = np.frombuffer(
            b"".join(row["embedding"] for row in rows), dtype=np.float32
        ).reshape(len(rows), -1)
        scores = vecs @ query_vec
        for row, score in zip(rows, scores):
            file_id = row["file_id"]
            if file_id in exclude_ids:
                continue
            score = float(score)
            if score >= min_score and (file_id not in best or score > best[file_id][0]):
                best[file_id] = (score, row["id"])

    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    if not ranked:
        return []

    chunk_ids = [chunk_id for _, (_, chunk_id) in ranked]
    placeholders = ",".join("?" * len(chunk_ids))
    text_rows = conn.execute(
        f"SELECT id, chunk_text FROM file_chunks WHERE id IN ({placeholders})", chunk_ids
    ).fetchall()
    texts = {row["id"]: row["chunk_text"] for row in text_rows}
    return [
        (file_id, score, _make_snippet(texts.get(chunk_id, "")))
        for file_id, (score, chunk_id) in ranked
    ]


def hybrid_search(
    conn: sqlite3.Connection,
    keywords: list[str],
    extensions: Optional[list[str]] = None,
    recency_days: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    query_vec: Optional[np.ndarray] = None,
    semantic_min_score: float = 0.75,
    semantic_max_results: int = 10,
) -> list[SearchResult]:
    """ファイル名+本文のキーワード検索に、埋め込みによる意味検索を組み合わせる。

    キーワードで一致した結果を優先して返し、キーワードで一致しなかった
    ファイルの中から意味的に類似したものを補う形で追加する(Phase 0の
    「ファイル名に手がかりがない場合は見つからない」という制約を、
    埋め込みが利用可能な場合に限り緩和する)。
    """
    name_match_map: dict[int, set[str]] = {}
    content_match_map: dict[int, set[str]] = {}
    for keyword in keywords:
        for file_id in _match_keyword(conn, keyword):
            name_match_map.setdefault(file_id, set()).add(keyword)
        for file_id in _match_content_keyword(conn, keyword):
            content_match_map.setdefault(file_id, set()).add(keyword)

    keyword_ids = list(set(name_match_map) | set(content_match_map))
    rows_by_id = _fetch_rows(conn, keyword_ids, extensions, recency_days)
    if not rows_by_id and keyword_ids and (extensions or recency_days is not None):
        rows_by_id = _fetch_rows(conn, keyword_ids, None, None)

    keyword_results = []
    for file_id, row in rows_by_id.items():
        matched = name_match_map.get(file_id, set()) | content_match_map.get(file_id, set())
        keyword_results.append(SearchResult(
            id=row["id"], name=row["name"], path=row["path"], dir=row["dir"],
            ext=row["ext"], size=row["size"], mtime=row["mtime"],
            matched_keywords=sorted(matched),
            matched_in_name=file_id in name_match_map,
            matched_in_content=file_id in content_match_map,
        ))
    keyword_results.sort(key=lambda r: (-len(r.matched_keywords), -r.mtime))
    keyword_results = keyword_results[:limit]

    remaining = limit - len(keyword_results)
    if remaining <= 0 or query_vec is None:
        return keyword_results

    exclude_ids = set(name_match_map) | set(content_match_map) | {r.id for r in keyword_results}
    semantic_hits = semantic_search(conn, query_vec, exclude_ids, semantic_max_results, semantic_min_score)
    if not semantic_hits:
        return keyword_results

    sem_ids = [fid for fid, _, _ in semantic_hits]
    sem_rows_by_id = _fetch_rows(conn, sem_ids, extensions, recency_days)
    score_and_snippet = {fid: (score, snippet) for fid, score, snippet in semantic_hits}

    semantic_results = []
    for file_id, row in sem_rows_by_id.items():
        score, snippet = score_and_snippet[file_id]
        semantic_results.append(SearchResult(
            id=row["id"], name=row["name"], path=row["path"], dir=row["dir"],
            ext=row["ext"], size=row["size"], mtime=row["mtime"],
            semantic_score=score, snippet=snippet,
        ))
    semantic_results.sort(key=lambda r: -(r.semantic_score or 0))
    return keyword_results + semantic_results[:remaining]
