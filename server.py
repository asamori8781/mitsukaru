"""FastAPIアプリ本体。設定/スキャン/検索/ファイル操作のAPIと静的ファイル配信。"""
from __future__ import annotations

import csv
import dataclasses
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_client
import config
import db
import embedder
import indexer
import search

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ミツカル")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_config: Optional[config.AppConfig] = None
_embedder_instance: Optional[embedder.Embedder] = None
_embedder_lock = threading.Lock()


# ---- リクエストボディ ----

class AIConfigIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: int = config.DEFAULT_TIMEOUT_SEC
    skip: bool = False


class TestConnectionIn(BaseModel):
    base_url: str
    api_key: str
    model: str
    timeout_sec: int = config.DEFAULT_TIMEOUT_SEC


class SettingsIn(BaseModel):
    scan_all_drives: bool
    target_folders: list[str]
    exclude_folders: list[str]
    exclude_extensions: list[str]
    diff_interval_minutes: int


class ScanStartIn(BaseModel):
    mode: str  # "all_drives" | "folders"
    folders: list[str] = []


class SearchIn(BaseModel):
    query: str


class LocalSearchIn(BaseModel):
    keywords: list[str]
    extensions: list[str] = []
    recency_days: Optional[int] = None


class OpenPathIn(BaseModel):
    path: str


class UsageLogIn(BaseModel):
    query: str
    keyword_count: int
    hit_count: int
    opened_file: bool


# ---- 設定の読み書き(プロセス内キャッシュ) ----

def _get_config() -> config.AppConfig:
    global _config
    if _config is None:
        _config = config.load_config()
    return _config


def _save_config(cfg: config.AppConfig) -> None:
    global _config
    _config = cfg
    config.save_config(cfg)


def _get_embedder() -> Optional[embedder.Embedder]:
    """埋め込みモデルのロード(重いため一度だけ行い使い回す)。未ダウンロード/
    ロード失敗の場合はNoneを返し、呼び出し側はキーワード検索のみに縮退する。"""
    global _embedder_instance
    with _embedder_lock:
        if _embedder_instance is not None:
            return _embedder_instance
        if not embedder.is_downloaded(config.MODELS_DIR):
            return None
        try:
            _embedder_instance = embedder.Embedder(config.MODELS_DIR)
        except embedder.EmbedderError:
            return None
        return _embedder_instance


# ---- 差分スキャンの自動起動 ----

def _on_diff_scan_finished(_summary: dict) -> None:
    cfg = _get_config()
    cfg.state.last_diff_scan_at = time.time()
    _save_config(cfg)


def _on_full_scan_finished(_summary: dict) -> None:
    cfg = _get_config()
    now = time.time()
    cfg.state.last_full_scan_at = now
    cfg.state.last_diff_scan_at = now
    _save_config(cfg)


def _trigger_startup_diff_scan() -> None:
    cfg = _get_config()
    roots = indexer.resolve_scan_roots(cfg.scan.scan_all_drives, cfg.scan.target_folders)
    if roots and not indexer.is_running():
        indexer.start_scan(
            "diff", roots, cfg.scan.exclude_folders, cfg.scan.exclude_extensions,
            config.DB_PATH, on_finish=_on_diff_scan_finished,
        )


def _maybe_trigger_stale_diff_scan(cfg: config.AppConfig) -> None:
    last = cfg.state.last_diff_scan_at or 0
    interval_sec = max(cfg.scan.diff_interval_minutes, 1) * 60
    if time.time() - last < interval_sec or indexer.is_running():
        return
    roots = indexer.resolve_scan_roots(cfg.scan.scan_all_drives, cfg.scan.target_folders)
    if roots:
        indexer.start_scan(
            "diff", roots, cfg.scan.exclude_folders, cfg.scan.exclude_extensions,
            config.DB_PATH, on_finish=_on_diff_scan_finished,
        )


@app.on_event("startup")
def _on_startup() -> None:
    config.ensure_dirs()
    db.check_sqlite_version()
    db.init_schema(config.DB_PATH)
    _get_config()
    _trigger_startup_diff_scan()


# ---- 状態・設定系エンドポイント ----

@app.get("/api/status")
def api_status() -> dict:
    cfg = _get_config()
    needs_folder_setup = (
        not cfg.scan.scan_all_drives
        and not cfg.scan.target_folders
        and cfg.state.last_full_scan_at is None
    )
    return {
        "config_exists": config.config_exists(),
        "mock_mode": cfg.ai.mock_mode,
        "needs_folder_setup": needs_folder_setup,
    }


@app.get("/api/config")
def api_get_config() -> dict:
    return dataclasses.asdict(_get_config().ai)


@app.post("/api/config")
def api_post_config(body: AIConfigIn) -> dict:
    cfg = _get_config()
    if body.skip:
        cfg.ai = config.AIConfig(mock_mode=True)
    else:
        if not body.base_url.strip() or not body.model.strip():
            raise HTTPException(status_code=400, detail="ベースURLとモデル名は必須です。")
        cfg.ai = config.AIConfig(
            base_url=body.base_url.strip(),
            api_key=body.api_key,
            model=body.model.strip(),
            timeout_sec=body.timeout_sec or config.DEFAULT_TIMEOUT_SEC,
            mock_mode=False,
        )
    _save_config(cfg)
    return {"ok": True}


@app.post("/api/config/test-connection")
def api_test_connection(body: TestConnectionIn) -> dict:
    return ai_client.test_connection(
        body.base_url.strip(), body.api_key, body.model.strip(),
        body.timeout_sec or config.DEFAULT_TIMEOUT_SEC, config.API_LOG_PATH,
    )


@app.get("/api/settings")
def api_get_settings() -> dict:
    return dataclasses.asdict(_get_config().scan)


@app.post("/api/settings")
def api_post_settings(body: SettingsIn) -> dict:
    cfg = _get_config()
    cfg.scan = config.ScanConfig(
        scan_all_drives=body.scan_all_drives,
        target_folders=body.target_folders,
        exclude_folders=body.exclude_folders,
        exclude_extensions=body.exclude_extensions,
        diff_interval_minutes=max(1, body.diff_interval_minutes),
    )
    _save_config(cfg)
    return {"ok": True}


@app.get("/api/stats")
def api_stats() -> dict:
    cfg = _get_config()
    summary = indexer.compute_summary(config.DB_PATH)
    conn = db.get_connection(config.DB_PATH)
    try:
        content_indexed_count = conn.execute(
            "SELECT COUNT(*) FROM file_content WHERE error IS NULL"
        ).fetchone()[0]
        embedded_file_count = conn.execute(
            "SELECT COUNT(DISTINCT file_id) FROM file_chunks"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        **summary,
        "last_full_scan_at": cfg.state.last_full_scan_at,
        "last_diff_scan_at": cfg.state.last_diff_scan_at,
        "content_indexed_count": content_indexed_count,
        "embedded_file_count": embedded_file_count,
        "last_content_index_at": cfg.phase1.last_content_index_at,
        # ここでモデルの実ロード(_get_embedder)をすると、設定画面を開くたびに
        # 100MB級のONNXロードで数秒固まるため、ファイル存在チェックのみにする。
        # 実際のロードは検索時に遅延して行われる。
        "semantic_search_available": embedder.is_downloaded(config.MODELS_DIR),
    }


# ---- スキャン系エンドポイント ----

@app.post("/api/scan/start")
def api_scan_start(body: ScanStartIn) -> dict:
    cfg = _get_config()
    if body.mode == "all_drives":
        roots = indexer.get_all_drives()
        cfg.scan.scan_all_drives = True
    else:
        roots = [f for f in body.folders if os.path.isdir(f)]
        cfg.scan.scan_all_drives = False
        cfg.scan.target_folders = roots
    _save_config(cfg)

    if not roots:
        raise HTTPException(status_code=400, detail="有効な対象フォルダがありません。")

    started = indexer.start_scan(
        "full", roots, cfg.scan.exclude_folders, cfg.scan.exclude_extensions,
        config.DB_PATH, on_finish=_on_full_scan_finished,
    )
    if not started:
        raise HTTPException(status_code=409, detail="既にスキャンが実行中です。")
    return {"ok": True}


@app.get("/api/scan/progress")
def api_scan_progress() -> dict:
    return indexer.get_progress()


@app.post("/api/scan/cancel")
def api_scan_cancel() -> dict:
    indexer.cancel_scan()
    return {"ok": True}


# ---- Phase1: コンテンツインデックス作成エンドポイント ----

def _on_content_index_finished() -> None:
    global _embedder_instance
    cfg = _get_config()
    cfg.phase1.last_content_index_at = time.time()
    _save_config(cfg)
    # 新しく埋め込みモデルがダウンロードされた可能性があるため、次回検索時に
    # 再ロードを試みられるようキャッシュを破棄する
    with _embedder_lock:
        _embedder_instance = None


@app.post("/api/content-index/start")
def api_content_index_start() -> dict:
    started = indexer.start_content_index(
        config.DB_PATH, config.MODELS_DIR, on_finish=_on_content_index_finished,
        error_log_path=config.LOGS_DIR / "content_index_errors.jsonl",
    )
    if not started:
        raise HTTPException(status_code=409, detail="既にコンテンツインデックスを作成中です。")
    return {"ok": True}


@app.get("/api/content-index/progress")
def api_content_index_progress() -> dict:
    return indexer.get_content_progress()


@app.post("/api/content-index/cancel")
def api_content_index_cancel() -> dict:
    indexer.cancel_content_index()
    return {"ok": True}


# ---- 検索系エンドポイント ----

def _expand_query(cfg: config.AppConfig, query: str) -> tuple[list[str], list[str], Optional[int], bool, str]:
    if cfg.ai.mock_mode:
        return query.split(), [], None, True, "モックモード動作中のため、入力文をそのままキーワードとして検索しました。"
    try:
        result = ai_client.expand_keywords(
            cfg.ai.base_url, cfg.ai.api_key, cfg.ai.model, cfg.ai.timeout_sec, query, config.API_LOG_PATH,
        )
        return result.keywords, result.extensions, result.recency_days, False, ""
    except ai_client.AIClientError as e:
        return query.split(), [], None, True, f"AIによるキーワード展開に失敗したため、入力文をそのまま検索しました({e})"


@app.post("/api/search")
def api_search(body: SearchIn) -> dict:
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="検索文を入力してください。")

    cfg = _get_config()
    keywords, extensions, recency_days, fallback_used, fallback_reason = _expand_query(cfg, query)
    _maybe_trigger_stale_diff_scan(cfg)

    query_vec = None
    emb = _get_embedder()
    if emb is not None:
        try:
            query_vec = emb.embed_query(query)
        except Exception:
            query_vec = None  # 意味検索は失敗してもキーワード検索の結果は返す

    conn = db.get_connection(config.DB_PATH)
    try:
        results = search.hybrid_search(
            conn, keywords, extensions or None, recency_days,
            query_vec=query_vec,
            semantic_min_score=cfg.phase1.semantic_min_score,
            semantic_max_results=cfg.phase1.semantic_max_results,
        )
    finally:
        conn.close()

    return {
        "keywords": keywords,
        "extensions": extensions,
        "recency_days": recency_days,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "semantic_search_used": query_vec is not None,
        "results": [dataclasses.asdict(r) for r in results],
    }


@app.post("/api/search/local")
def api_search_local(body: LocalSearchIn) -> dict:
    keywords = [k.strip() for k in body.keywords if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="キーワードを入力してください。")

    # キーワード編集後の再検索はAPI(生成AI・埋め込み)を一切呼ばない。
    # 本文の全文検索(FTS)自体はローカルDB操作のみで完結するため含める。
    conn = db.get_connection(config.DB_PATH)
    try:
        results = search.hybrid_search(conn, keywords, body.extensions or None, body.recency_days)
    finally:
        conn.close()

    return {"results": [dataclasses.asdict(r) for r in results]}


# ---- ファイル操作系エンドポイント ----

@app.post("/api/open-file")
def api_open_file(body: OpenPathIn) -> dict:
    if not os.path.exists(body.path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません。")
    if not hasattr(os, "startfile"):
        raise HTTPException(status_code=501, detail="この環境ではファイルを開く機能に対応していません(Windows専用)。")
    os.startfile(body.path)  # type: ignore[attr-defined]
    return {"ok": True}


@app.post("/api/open-folder")
def api_open_folder(body: OpenPathIn) -> dict:
    if not os.path.exists(body.path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません。")
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="この環境ではフォルダを開く機能に対応していません(Windows専用)。")
    subprocess.run(["explorer", "/select,", body.path])
    return {"ok": True}


# ---- 利用ログ ----

def _append_usage_log(query: str, keyword_count: int, hit_count: int, opened_file: bool) -> None:
    config.ensure_dirs()
    is_new = not config.USAGE_LOG_PATH.exists()
    with open(config.USAGE_LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["検索日時", "入力クエリ", "展開語数", "ヒット件数", "開いたファイルの有無"])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            query, keyword_count, hit_count, "あり" if opened_file else "なし",
        ])


@app.post("/api/usage-log")
def api_usage_log(body: UsageLogIn) -> dict:
    _append_usage_log(body.query, body.keyword_count, body.hit_count, body.opened_file)
    return {"ok": True}


# ---- 画面 ----

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
