"""pytest 公共准备：把数据目录重定向到临时目录，避免污染源码树。"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent
TMP_DATA = tempfile.mkdtemp(prefix="lineagex-tests-")

os.environ["LINEAGEX_DATA_DIR"] = TMP_DATA
os.environ["LINEAGEX_SAMPLE_DIR"] = str(PROJECT_DIR / "sample_data")
sys.path.insert(0, str(PROJECT_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def sample_dir() -> pathlib.Path:
    return PROJECT_DIR / "sample_data"


@pytest.fixture(scope="session")
def schemas(sample_dir):
    from app import pipeline

    return pipeline.load_schemas(sample_dir)


@pytest.fixture(scope="session")
def sample_files(sample_dir):
    from app import pipeline

    return pipeline.load_sample_files(sample_dir)


@pytest.fixture(scope="session")
def analysis(sample_files, schemas):
    from app import pipeline

    return pipeline.analyze(sample_files, schemas)


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def sample_run_id(client) -> str:
    res = client.post("/api/runs", json={"mode": "sample"})
    assert res.status_code == 200, res.text
    return res.json()["run_id"]
