"""路径与常量。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "lineagex · SQL 列级血缘与迁移影响面"
APP_SHORT = "lineagex"
APP_DESC = "改一个源表字段之前，先看清它会波及哪些下游视图与任务，并拿到带顺序的迁移清单。"

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

DATA_DIR = Path(os.environ.get("LINEAGEX_DATA_DIR", PROJECT_DIR / "data"))
DB_PATH = DATA_DIR / "lineagex.db"
SAMPLE_DIR = Path(os.environ.get("LINEAGEX_SAMPLE_DIR", PROJECT_DIR / "sample_data"))

SEED_VERSION = "2026-11-14.1"
