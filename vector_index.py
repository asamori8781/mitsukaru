"""意味検索の高速化(IVF: 転置ファイル方式の近似最近傍探索)。

チャンク埋め込みを球面k-meansでクラスタリングして各チャンクにcluster_idを
付与し、検索時はクエリベクトルに近い上位クラスタのみを走査する。追加の
ネイティブ依存は増やさない(numpy+標準sqlite3のみ)方針のための純numpy実装。

正確性の担保:
- cluster_idがNULLのチャンク(インデックス構築後に追加されたもの、次元の
  異なるモデルで作られたもの等)は検索時に常に走査対象へ含めるため、
  検索漏れは起きない。未割当が増えると速度が落ちるだけで、閾値を超えると
  自動で再構築される。
- 再構築は「インデックスを無効化(meta削除)→全チャンク再割当→完了後に
  メタとセントロイドを公開」の順で行う。途中でキャンセル・中断されても
  「未加速(総当たり)に戻る」だけで、誤った近似結果にはならない。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, Optional

import numpy as np

# これ未満のチャンク数では総当たりで十分速く、インデックス維持の方が高くつく
MIN_CHUNKS_FOR_INDEX = 5000
# k-means学習に使うサンプル数の上限(全件学習は不要。等間隔サンプリング)
TRAIN_SAMPLE_MAX = 50_000
KMEANS_ITERS = 10
KMEANS_BATCH = 8192
SCAN_BATCH = 5000
ASSIGN_BATCH = 5000
# 構築時からチャンク数がこの倍率を超えたら再構築(分布の変化に追従)
REBUILD_GROWTH_RATIO = 1.5
# 未割当(NULL)チャンクがこの割合を超えたら再構築(走査量が増えて効果が薄れるため)
REBUILD_NULL_RATIO = 0.3


def choose_k(total: int) -> int:
    """クラスタ数。IVFの定石であるsqrt(N)を基準に、極端な値を避ける。"""
    return max(8, min(1024, int(total ** 0.5)))


def nprobe_for(k: int) -> int:
    """検索時に走査するクラスタ数。全体の約1/8を見れば上位ヒットはほぼ拾える。"""
    return max(4, k // 8)


def get_meta(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT dim, built_at, indexed_chunk_count FROM vector_index_meta WHERE id=1").fetchone()


def load_centroids(conn: sqlite3.Connection, dim: Optional[int] = None) -> Optional[np.ndarray]:
    """公開済みのクラスタ中心を(K, dim)行列で返す。行番号=cluster_id。

    dimを指定した場合、構築時の次元と一致しなければNone(モデル差し替え後の
    クエリに古いインデックスを適用しない)。metaが無い=インデックス無効。
    """
    meta = get_meta(conn)
    if meta is None:
        return None
    if dim is not None and meta["dim"] != dim:
        return None
    rows = conn.execute("SELECT cluster_id, centroid FROM vector_centroids ORDER BY cluster_id").fetchall()
    if not rows or rows[-1]["cluster_id"] != len(rows) - 1:
        return None  # 欠番があれば壊れているとみなして使わない
    return np.frombuffer(b"".join(row["centroid"] for row in rows), dtype=np.float32).reshape(len(rows), -1)


def top_clusters(centroids: np.ndarray, query_vec: np.ndarray) -> list[int]:
    sims = centroids @ query_vec
    nprobe = nprobe_for(len(centroids))
    return [int(c) for c in np.argsort(-sims)[:nprobe]]


def assign_clusters(centroids: Optional[np.ndarray], vecs: np.ndarray) -> list:
    """新規チャンクの埋め込みを既存クラスタへ割り当てる。

    インデックス未構築・次元不一致の場合はNone(未割当)を返す。未割当は
    検索時に常に走査されるので安全側。次の再構築時にまとめて割り当てられる。
    """
    if centroids is None or vecs.ndim != 2 or vecs.shape[1] != centroids.shape[1]:
        return [None] * len(vecs)
    return _batched_labels(np.asarray(vecs, dtype=np.float32), centroids).tolist()


def _count_chunks(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM file_chunks").fetchone()[0]


def needs_rebuild(conn: sqlite3.Connection) -> bool:
    total = _count_chunks(conn)
    if total < MIN_CHUNKS_FOR_INDEX:
        return False
    meta = get_meta(conn)
    if meta is None:
        return True
    nulls = conn.execute("SELECT COUNT(*) FROM file_chunks WHERE cluster_id IS NULL").fetchone()[0]
    if nulls > total * REBUILD_NULL_RATIO:
        return True
    if total > meta["indexed_chunk_count"] * REBUILD_GROWTH_RATIO:
        return True
    return False


def _dominant_dim(conn: sqlite3.Connection) -> Optional[int]:
    """最も多い埋め込みバイト長から次元を推定する(モデル差し替えで
    異なる次元が混在していても、多数派に合わせてインデックスを組む)。"""
    row = conn.execute(
        "SELECT length(embedding) AS bl, COUNT(*) AS cnt FROM file_chunks "
        "GROUP BY bl ORDER BY cnt DESC LIMIT 1"
    ).fetchone()
    if row is None or row["bl"] is None or row["bl"] % 4 != 0:
        return None
    return row["bl"] // 4


def _batched_labels(vecs: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    labels = np.empty(len(vecs), dtype=np.int64)
    for start in range(0, len(vecs), KMEANS_BATCH):
        chunk = vecs[start:start + KMEANS_BATCH]
        labels[start:start + KMEANS_BATCH] = np.argmax(chunk @ centroids.T, axis=1)
    return labels


def _spherical_kmeans(
    sample: np.ndarray, k: int, cancel_check: Optional[Callable[[], bool]]
) -> Optional[np.ndarray]:
    """コサイン類似度ベースのk-means。埋め込みはL2正規化済みなので、
    平均→再正規化(球面k-means)で中心を更新する。キャンセル時はNone。"""
    rng = np.random.default_rng(12345)
    n, dim = sample.shape
    k = min(k, n)
    centroids = sample[rng.choice(n, size=k, replace=False)].copy()
    for _ in range(KMEANS_ITERS):
        if cancel_check and cancel_check():
            return None
        labels = _batched_labels(sample, centroids)
        sums = np.zeros((k, dim), dtype=np.float64)
        np.add.at(sums, labels, sample)
        counts = np.bincount(labels, minlength=k)
        empty = counts == 0
        if empty.any():
            # 空クラスタはランダムなサンプル点で再初期化して縮退を防ぐ
            sums[empty] = sample[rng.choice(n, size=int(empty.sum()))]
        norms = np.linalg.norm(sums, axis=1, keepdims=True)
        centroids = (sums / np.maximum(norms, 1e-12)).astype(np.float32)
    return centroids


def _sample_embeddings(conn: sqlite3.Connection, dim: int, total: int) -> np.ndarray:
    step = max(1, total // TRAIN_SAMPLE_MAX)
    cursor = conn.execute(
        "SELECT embedding FROM file_chunks WHERE length(embedding)=?", (dim * 4,)
    )
    bufs: list[bytes] = []
    i = 0
    while True:
        rows = cursor.fetchmany(SCAN_BATCH)
        if not rows:
            break
        for row in rows:
            if i % step == 0:
                bufs.append(row[0])
            i += 1
    return np.frombuffer(b"".join(bufs), dtype=np.float32).reshape(-1, dim)


def build(
    conn: sqlite3.Connection,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> bool:
    """インデックスを(再)構築する。構築できた場合はTrue。

    処理中はmetaを消してインデックスを無効化する(検索は総当たりに退避)。
    全チャンクの再割当が終わってからメタ・セントロイドを公開するため、
    途中キャンセル・プロセス中断があっても不整合な近似は起きない。
    """
    dim = _dominant_dim(conn)
    if dim is None:
        return False
    total = conn.execute(
        "SELECT COUNT(*) FROM file_chunks WHERE length(embedding)=?", (dim * 4,)
    ).fetchone()[0]
    if total < MIN_CHUNKS_FOR_INDEX:
        return False

    sample = _sample_embeddings(conn, dim, total)
    if len(sample) == 0:
        return False
    centroids = _spherical_kmeans(sample, choose_k(total), cancel_check)
    if centroids is None:
        return False

    # 旧インデックスを無効化してから割当を進める(この間の検索は総当たり)
    conn.execute("DELETE FROM vector_index_meta")
    conn.execute("DELETE FROM vector_centroids")
    conn.commit()

    assigned = 0
    last_id = 0
    while True:
        if cancel_check and cancel_check():
            return False
        rows = conn.execute(
            "SELECT id, embedding FROM file_chunks WHERE id > ? AND length(embedding)=? "
            "ORDER BY id LIMIT ?",
            (last_id, dim * 4, ASSIGN_BATCH),
        ).fetchall()
        if not rows:
            break
        vecs = np.frombuffer(b"".join(row["embedding"] for row in rows), dtype=np.float32).reshape(len(rows), dim)
        labels = _batched_labels(vecs, centroids)
        conn.executemany(
            "UPDATE file_chunks SET cluster_id=? WHERE id=?",
            zip(labels.tolist(), (row["id"] for row in rows)),
        )
        conn.commit()
        last_id = rows[-1]["id"]
        assigned += len(rows)
        if progress_cb:
            progress_cb(assigned, total)

    conn.executemany(
        "INSERT INTO vector_centroids(cluster_id, centroid) VALUES (?, ?)",
        [(i, c.tobytes()) for i, c in enumerate(centroids)],
    )
    conn.execute(
        "INSERT INTO vector_index_meta(id, dim, built_at, indexed_chunk_count) VALUES (1, ?, ?, ?)",
        (dim, time.time(), assigned),
    )
    conn.commit()
    return True


def maybe_rebuild(
    conn: sqlite3.Connection,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> bool:
    if not needs_rebuild(conn):
        return False
    return build(conn, cancel_check, progress_cb)
