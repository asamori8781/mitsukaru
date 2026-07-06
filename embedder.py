"""ローカル埋め込みモデル(Phase 1: 意味検索用)。

モデルは初回起動時にHugging Faceからdata/models/配下へダウンロードする。
抽出済みテキストはこのモジュール内でのみ処理し、外部(API)へは一切送信
しない(ai_client.pyの「AIはキーワード展開のみに使用」という方針とは
別軸の、完全ローカルな仕組みとして独立させている)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import numpy as np
import requests

MODEL_NAME = "multilingual-e5-small"
MODEL_DIM = 384
MAX_SEQ_LEN = 512
DOWNLOAD_CHUNK = 1024 * 1024

# Xenova配布のONNX変換版(量子化済み、~100MB程度)を利用する。
# URLが変わってダウンロードに失敗する場合はここを更新すること。
MODEL_FILES = {
    "model.onnx": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx",
    "tokenizer.json": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/tokenizer.json",
}


class EmbedderError(Exception):
    pass


def model_dir(models_root: Path) -> Path:
    return models_root / MODEL_NAME


def is_downloaded(models_root: Path) -> bool:
    d = model_dir(models_root)
    return all((d / name).exists() for name in MODEL_FILES)


def download_model(
    models_root: Path,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    """モデル一式をダウンロードする。既に存在するファイルはスキップする。"""
    d = model_dir(models_root)
    d.mkdir(parents=True, exist_ok=True)
    for name, url in MODEL_FILES.items():
        dest = d / name
        if dest.exists():
            continue
        tmp_dest = d / (name + ".part")
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(tmp_dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            on_progress(name, downloaded, total)
        except requests.exceptions.RequestException as e:
            tmp_dest.unlink(missing_ok=True)
            raise EmbedderError(
                f"埋め込みモデルのダウンロードに失敗しました({name}): {e}"
            ) from e
        tmp_dest.replace(dest)


EMBED_BATCH_SIZE = 16


class Embedder:
    """埋め込みモデルのロードと推論。ロードが重いためプロセス内で使い回すこと。"""

    def __init__(self, models_root: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if not is_downloaded(models_root):
            raise EmbedderError("埋め込みモデルがダウンロードされていません。")
        d = model_dir(models_root)
        self._tokenizer = Tokenizer.from_file(str(d / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=MAX_SEQ_LEN)
        # パディングトークンはモデル系統により異なる(XLM-R系は<pad>、BERT系は[PAD])
        pad_id = 0
        pad_token = "[PAD]"
        for candidate in ("<pad>", "[PAD]"):
            token_id = self._tokenizer.token_to_id(candidate)
            if token_id is not None:
                pad_id, pad_token = token_id, candidate
                break
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)
        self._session = ort.InferenceSession(
            str(d / "model.onnx"), providers=["CPUExecutionProvider"]
        )
        # モデルによって必須入力が異なる(BERT系はtoken_type_idsも必須)ため、
        # 実際のグラフ定義から入力名・出力名を取得して合わせる。
        self._input_names = {inp.name for inp in self._session.get_inputs()}
        output_names = [out.name for out in self._session.get_outputs()]
        self._output_name = (
            "last_hidden_state" if "last_hidden_state" in output_names else output_names[0]
        )

    def _encode(self, texts: list[str]) -> np.ndarray:
        results = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start:start + EMBED_BATCH_SIZE]
            encodings = self._tokenizer.encode_batch(batch)
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            feed = {"input_ids": input_ids, "attention_mask": attention_mask}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            try:
                outputs = self._session.run([self._output_name], feed)
            except Exception as e:
                raise EmbedderError(f"埋め込みモデルの推論に失敗しました: {e}") from e
            last_hidden_state = outputs[0]  # (batch, seq, hidden)
            # attentionでマスクされた位置(パディング)を除いた平均プーリング
            mask = attention_mask[:, :, None].astype(np.float32)
            summed = (last_hidden_state * mask).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            pooled = summed / counts
            norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            results.append((pooled / norms).astype(np.float32))
        return np.concatenate(results, axis=0)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        # e5系モデルの規約: 索引対象の文書側には "passage: " を付与する
        return self._encode([f"passage: {t}" for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        # e5系モデルの規約: 検索クエリ側には "query: " を付与する
        return self._encode([f"query: {text}"])[0]


def pack_vector(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def top_k_similar(query_vec: np.ndarray, chunk_vecs: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """総当たりコサイン類似度。chunk_vecsは行ごとにL2正規化済みであること。

    戻り値は (類似度降順のインデックス配列, 対応するスコア配列)。
    """
    if len(chunk_vecs) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
    scores = chunk_vecs @ query_vec
    k = min(k, len(scores))
    top_idx = np.argpartition(-scores, k - 1)[:k]
    order = top_idx[np.argsort(-scores[top_idx])]
    return order, scores[order]
