"""seed：把样例脚本与 schema 写库，并跑一次基线解析。

幂等策略：meta.seed_version 命中且脚本数、schema 数一致时直接跳过；
重复执行不会产生脏数据，也不会重复插入脚本行。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import pipeline, settings, store


def ensure_seed(conn, sample_dir: Path | None = None, seed_run: bool = True) -> dict:
    sample_dir = sample_dir or settings.SAMPLE_DIR
    sample_scripts = sorted((sample_dir / "sql").glob("*.sql"))
    broken_scripts = sorted((sample_dir / "sql_broken").glob("*.sql"))
    schema_files = sorted((sample_dir / "schemas").glob("*.json"))

    version = store.get_meta(conn, "seed_version")
    counts = (
        conn.execute("SELECT COUNT(*) FROM scripts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM schemas").fetchone()[0],
    )
    expected = (len(sample_scripts) + len(broken_scripts), len(schema_files))
    if version == settings.SEED_VERSION and counts == expected:
        return {
            "seeded": False,
            "reason": "seed_version 命中，跳过",
            "scripts": counts[0],
            "schemas": counts[1],
            "baseline_run_id": store.get_meta(conn, "baseline_run_id"),
        }

    with conn:
        conn.execute("DELETE FROM scripts")
        order = 0
        for path in sample_scripts + broken_scripts:
            set_name = "sample" if path.parent.name == "sql" else "broken"
            order += 1
            conn.execute(
                "INSERT INTO scripts(filename, set_name, sql, sort_order) VALUES(?,?,?,?)",
                (path.name, set_name, path.read_text(encoding="utf-8"), order),
            )
        conn.execute("DELETE FROM schemas")
        for path in schema_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schemas(table_name, columns_json, comment) VALUES(?,?,?)",
                (
                    data["table"],
                    json.dumps([c["name"] for c in data["columns"]], ensure_ascii=False),
                    data.get("comment", ""),
                ),
            )
        store.set_meta(conn, "seed_version", settings.SEED_VERSION)

    baseline = store.get_meta(conn, "baseline_run_id")
    if seed_run and not baseline:
        run_id, _ = pipeline.run_sample(conn, sample_dir)
        store.set_meta(conn, "baseline_run_id", run_id)
        baseline = run_id

    return {
        "seeded": True,
        "reason": "写入样例脚本与 schema",
        "scripts": len(sample_scripts) + len(broken_scripts),
        "schemas": len(schema_files),
        "baseline_run_id": baseline,
    }
