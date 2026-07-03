"""AI API呼び出しを隔離するモジュール。

送信するのはユーザーの検索文と展開指示プロンプトのみ。
ファイル名・パス・ファイル内容・スキャン結果は絶対に送信しない。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

SYSTEM_PROMPT = (
    "あなたはローカルファイル検索を補助するアシスタントです。"
    "ユーザーが入力した曖昧な検索文から、ファイル名やパスの一致に使えそうな"
    "キーワードを8〜15個考えてください。日本語表現・英語表現・略語・"
    "表記ゆれ(全角半角、カタカナ/ひらがな、送り仮名の違いなど)を含めてください。"
    "検索対象と推定されるファイル拡張子があれば、ドットから始まる形式"
    "(例: \".pdf\")でリストにしてください。わからない場合は空配列にしてください。"
    "検索対象と推定される期間があれば、直近何日以内かを数値にしてください。"
    "わからない場合はnullにしてください。"
    "出力は次の形式のJSONオブジェクトのみを返してください。"
    "前置き、説明文、コードフェンス(```)は一切付けないでください。"
    '{"keywords": ["文字列", ...], "extensions": ["文字列", ...], "recency_days": 数値またはnull}'
)

TEST_SYSTEM_PROMPT = "あなたは接続テスト用の応答者です。"
TEST_USER_PROMPT = "接続テストです。'OK'とだけ回答してください。"

MAX_KEYWORDS = 15


class AIClientError(Exception):
    """API呼び出し・応答解析に失敗した場合の例外(呼び出し側でフォールバックする)。"""


@dataclass
class ExpandResult:
    keywords: list[str]
    extensions: list[str]
    recency_days: Optional[int]


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _append_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _post_chat(base_url: str, api_key: str, model: str, timeout_sec: float, messages: list[dict]) -> tuple[str, float]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "stream": False}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.time()
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout_sec)
    except requests.exceptions.Timeout as e:
        raise AIClientError(f"APIへの接続がタイムアウトしました({timeout_sec}秒)。") from e
    except requests.exceptions.ConnectionError as e:
        raise AIClientError("APIに接続できませんでした。ベースURLを確認してください。") from e
    except requests.exceptions.RequestException as e:
        raise AIClientError(f"APIへのリクエストに失敗しました: {e}") from e
    elapsed = time.time() - started
    if resp.status_code >= 400:
        raise AIClientError(f"APIがエラーを返しました(HTTP {resp.status_code}): {resp.text[:200]}")
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise AIClientError("APIの応答形式が想定と異なります。") from e
    return content, elapsed


def expand_keywords(
    base_url: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    query: str,
    log_path: Path,
) -> ExpandResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]
    ts = time.time()
    try:
        content, elapsed = _post_chat(base_url, api_key, model, timeout_sec, messages)
    except AIClientError as e:
        _append_log(log_path, {
            "timestamp": ts, "type": "expand",
            "sent": {"system_prompt": SYSTEM_PROMPT, "query": query},
            "success": False, "error": str(e),
        })
        raise

    cleaned = _strip_code_fence(content)
    try:
        parsed = json.loads(cleaned)
        keywords = [str(k).strip() for k in parsed.get("keywords", []) if str(k).strip()]
        extensions = [str(e).strip() for e in parsed.get("extensions", []) if str(e).strip()]
        recency_raw = parsed.get("recency_days")
        recency_days = int(recency_raw) if isinstance(recency_raw, (int, float)) else None
        if not keywords:
            raise ValueError("keywordsが空です")
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
        _append_log(log_path, {
            "timestamp": ts, "type": "expand",
            "sent": {"system_prompt": SYSTEM_PROMPT, "query": query},
            "received": content, "success": False, "error": f"JSON解析に失敗しました: {e}",
        })
        raise AIClientError("AIの応答をJSONとして解釈できませんでした。") from e

    _append_log(log_path, {
        "timestamp": ts, "type": "expand",
        "sent": {"system_prompt": SYSTEM_PROMPT, "query": query},
        "received": content, "elapsed_sec": round(elapsed, 3), "success": True,
    })
    return ExpandResult(keywords=keywords[:MAX_KEYWORDS], extensions=extensions, recency_days=recency_days)


def test_connection(base_url: str, api_key: str, model: str, timeout_sec: float, log_path: Path) -> dict:
    messages = [
        {"role": "system", "content": TEST_SYSTEM_PROMPT},
        {"role": "user", "content": TEST_USER_PROMPT},
    ]
    ts = time.time()
    try:
        content, elapsed = _post_chat(base_url, api_key, model, timeout_sec, messages)
    except AIClientError as e:
        _append_log(log_path, {
            "timestamp": ts, "type": "test_connection",
            "sent": {"system_prompt": TEST_SYSTEM_PROMPT, "query": TEST_USER_PROMPT},
            "success": False, "error": str(e),
        })
        return {"success": False, "elapsed_sec": None, "message": str(e)}

    _append_log(log_path, {
        "timestamp": ts, "type": "test_connection",
        "sent": {"system_prompt": TEST_SYSTEM_PROMPT, "query": TEST_USER_PROMPT},
        "received": content, "elapsed_sec": round(elapsed, 3), "success": True,
    })
    return {
        "success": True,
        "elapsed_sec": round(elapsed, 2),
        "message": f"接続に成功しました(応答: {content.strip()[:50]})",
    }
