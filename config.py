"""アプリ設定(data/config.json)の読み書きと既定値、データ配置パスの解決。"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "mitsukaru.db"
API_LOG_PATH = LOGS_DIR / "api_log.jsonl"
USAGE_LOG_PATH = LOGS_DIR / "usage_log.csv"
MODELS_DIR = DATA_DIR / "models"

DEFAULT_EXCLUDE_FOLDERS = [
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "$Recycle.Bin",
    "System Volume Information",
    "AppData",
    "node_modules",
    ".git",
    "venv",
    "__pycache__",
]
DEFAULT_EXCLUDE_EXTENSIONS = [".exe", ".dll", ".sys", ".tmp", ".msi", ".cab", ".dmp", ".lnk"]
DEFAULT_DIFF_INTERVAL_MINUTES = 10
DEFAULT_TIMEOUT_SEC = 20


@dataclass
class AIConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    mock_mode: bool = True


@dataclass
class ScanConfig:
    scan_all_drives: bool = False
    target_folders: list[str] = field(default_factory=list)
    exclude_folders: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_FOLDERS))
    exclude_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_EXTENSIONS))
    diff_interval_minutes: int = DEFAULT_DIFF_INTERVAL_MINUTES


@dataclass
class ScanState:
    last_full_scan_at: Optional[float] = None
    last_diff_scan_at: Optional[float] = None


@dataclass
class Phase1Config:
    semantic_min_score: float = 0.75
    semantic_max_results: int = 10
    last_content_index_at: Optional[float] = None


@dataclass
class AppConfig:
    ai: AIConfig = field(default_factory=AIConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    state: ScanState = field(default_factory=ScanState)
    phase1: Phase1Config = field(default_factory=Phase1Config)


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _merge_known_fields(defaults: Any, raw: dict) -> dict:
    base = asdict(defaults)
    filtered = {k: v for k, v in raw.items() if k in base}
    base.update(filtered)
    return base


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        return AppConfig()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return AppConfig(
        ai=AIConfig(**_merge_known_fields(AIConfig(), raw.get("ai", {}))),
        scan=ScanConfig(**_merge_known_fields(ScanConfig(), raw.get("scan", {}))),
        state=ScanState(**_merge_known_fields(ScanState(), raw.get("state", {}))),
        phase1=Phase1Config(**_merge_known_fields(Phase1Config(), raw.get("phase1", {}))),
    )


def save_config(cfg: AppConfig) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
