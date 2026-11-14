"""SQLite 访问层（薄封装，无 ORM）。

表：
- meta                   seed 版本等键值
- scripts                样例脚本（sql/ 与 sql_broken/）
- schemas                表结构（列名 + 类型）
- runs                   每次解析任务
- run_scripts            每个脚本的解析结论（含错误位置）
- run_table_edges        表级血缘边
- run_column_edges       列级血缘边（含变换链 / star 标记 / 证据行号）
- run_cycles             环检测结果
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts (
  script_id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL UNIQUE,
  set_name TEXT NOT NULL,
  sql TEXT NOT NULL,
  sort_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schemas (
  table_name TEXT PRIMARY KEY,
  columns_json TEXT NOT NULL,
  comment TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_label TEXT NOT NULL,
  script_count INTEGER NOT NULL,
  parsed_count INTEGER NOT NULL,
  failed_count INTEGER NOT NULL,
  table_edge_count INTEGER NOT NULL,
  column_edge_count INTEGER NOT NULL,
  table_count INTEGER NOT NULL,
  cycle_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  note TEXT
);

CREATE TABLE IF NOT EXISTS run_scripts (
  run_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL,
  columns_json TEXT NOT NULL,
  line INTEGER NOT NULL,
  error_line INTEGER,
  error_col INTEGER,
  message TEXT,
  sql TEXT NOT NULL,
  PRIMARY KEY (run_id, filename)
);

CREATE TABLE IF NOT EXISTS run_table_edges (
  run_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  target_table TEXT NOT NULL,
  script TEXT NOT NULL,
  line INTEGER NOT NULL,
  target_kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rte_run ON run_table_edges(run_id);

CREATE TABLE IF NOT EXISTS run_column_edges (
  run_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_column TEXT NOT NULL,
  target_table TEXT NOT NULL,
  target_column TEXT NOT NULL,
  script TEXT NOT NULL,
  line INTEGER NOT NULL,
  expr TEXT NOT NULL,
  transforms_json TEXT NOT NULL,
  star_expanded INTEGER NOT NULL DEFAULT 0,
  union_branch INTEGER NOT NULL DEFAULT 0,
  target_kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rce_run ON run_column_edges(run_id);
CREATE INDEX IF NOT EXISTS idx_rce_src ON run_column_edges(run_id, source_table, source_column);

CREATE TABLE IF NOT EXISTS run_cycles (
  run_id TEXT NOT NULL,
  level TEXT NOT NULL,
  cycle_key TEXT NOT NULL,
  nodes_json TEXT NOT NULL,
  scripts_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rc_run ON run_cycles(run_id);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else settings.DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_run(conn: sqlite3.Connection, analysis: dict, source_label: str, duration_ms: int = 0) -> str:
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{abs(hash(source_label)) % 1000:03d}"
    created = now()
    cycles = analysis["cycles"]
    conn.execute(
        """INSERT INTO runs(run_id, created_at, source_label, script_count, parsed_count, failed_count,
                            table_edge_count, column_edge_count, table_count, cycle_count, status, duration_ms, note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            created,
            source_label,
            len(analysis["scripts"]),
            analysis["parsed_count"],
            analysis["failed_count"],
            len(analysis["table_edges"]),
            len(analysis["column_edges"]),
            len(analysis["table_levels"]),
            len(cycles),
            "ok" if analysis["failed_count"] == 0 else "with_errors",
            duration_ms,
            analysis.get("note"),
        ),
    )
    for s in analysis["scripts"]:
        conn.execute(
            """INSERT INTO run_scripts(run_id, filename, kind, target, status, columns_json, line,
                                       error_line, error_col, message, sql)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                s["filename"],
                s["kind"],
                s["target"],
                s["status"],
                json.dumps(s["columns"], ensure_ascii=False),
                s["line"] or 0,
                s.get("error_line"),
                s.get("error_col"),
                s.get("message"),
                s["sql"],
            ),
        )
    for e in analysis["table_edges"]:
        conn.execute(
            """INSERT INTO run_table_edges(run_id, source_table, target_table, script, line, target_kind)
               VALUES(?,?,?,?,?,?)""",
            (run_id, e["source_table"], e["target_table"], e["script"], e["line"], e["target_kind"]),
        )
    for e in analysis["column_edges"]:
        conn.execute(
            """INSERT INTO run_column_edges(run_id, source_table, source_column, target_table, target_column,
                                            script, line, expr, transforms_json, star_expanded, union_branch, target_kind)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                e["source_table"],
                e["source_column"],
                e["target_table"],
                e["target_column"],
                e["script"],
                e["line"],
                e["expr"],
                json.dumps(e["transforms"], ensure_ascii=False),
                1 if e["star_expanded"] else 0,
                e["union_branch"],
                e["target_kind"],
            ),
        )
    for c in cycles:
        conn.execute(
            "INSERT INTO run_cycles(run_id, level, cycle_key, nodes_json, scripts_json) VALUES(?,?,?,?,?)",
            (
                run_id,
                c["level"],
                c["key"],
                json.dumps(c["nodes"], ensure_ascii=False),
                json.dumps(c.get("scripts", []), ensure_ascii=False),
            ),
        )
    conn.commit()
    return run_id


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def load_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    scripts = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM run_scripts WHERE run_id = ? ORDER BY filename", (run_id,)
        ).fetchall()
    ]
    for s in scripts:
        s["columns"] = json.loads(s["columns_json"])
    table_edges = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM run_table_edges WHERE run_id = ? ORDER BY target_table, source_table, script, line",
            (run_id,),
        ).fetchall()
    ]
    column_edges = [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM run_column_edges WHERE run_id = ?
               ORDER BY target_table, target_column, source_table, source_column""",
            (run_id,),
        ).fetchall()
    ]
    for e in column_edges:
        e["transforms"] = json.loads(e["transforms_json"])
        e["star_expanded"] = bool(e["star_expanded"])
    cycles = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM run_cycles WHERE run_id = ? ORDER BY level, cycle_key", (run_id,)
        ).fetchall()
    ]
    for c in cycles:
        c["nodes"] = json.loads(c["nodes_json"])
        c["scripts"] = json.loads(c["scripts_json"])
        c["key"] = c["cycle_key"]
    return {
        "run": dict(run),
        "scripts": scripts,
        "table_edges": table_edges,
        "column_edges": column_edges,
        "cycles": cycles,
    }


def load_schemas(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in conn.execute("SELECT table_name, columns_json FROM schemas ORDER BY table_name").fetchall():
        out[row["table_name"]] = json.loads(row["columns_json"])
    return out


def list_scripts(conn: sqlite3.Connection, set_name: str | None = None) -> list[dict]:
    if set_name:
        rows = conn.execute(
            "SELECT * FROM scripts WHERE set_name = ? ORDER BY sort_order, filename", (set_name,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM scripts ORDER BY sort_order, filename").fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    return {
        "sample_scripts": one("SELECT COUNT(*) FROM scripts WHERE set_name = 'sample'"),
        "broken_scripts": one("SELECT COUNT(*) FROM scripts WHERE set_name = 'broken'"),
        "schema_tables": one("SELECT COUNT(*) FROM schemas"),
        "runs": one("SELECT COUNT(*) FROM runs"),
        "latest_run": (
            dict(conn.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone())
            if one("SELECT COUNT(*) FROM runs")
            else None
        ),
    }
