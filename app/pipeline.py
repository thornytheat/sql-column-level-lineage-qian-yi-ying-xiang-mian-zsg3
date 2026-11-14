"""编排层：读样例 → 解析 → 建图 → 算层级/环 → 落库。

核心规则全部在 lineage / graph / migrate / changes 纯函数模块里，这里只做组合与持久化。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import changes, graph, lineage, settings, store

SAMPLE_LABEL = "样例仓库（12 个脚本）"
SAMPLE_WITH_BROKEN_LABEL = "样例仓库 + 1 个坏脚本（13 个脚本）"


def load_schemas(sample_dir: Path) -> dict[str, list[str]]:
    schemas: dict[str, list[str]] = {}
    for path in sorted((sample_dir / "schemas").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        schemas[data["table"]] = [c["name"] for c in data["columns"]]
    return schemas


def load_sample_files(sample_dir: Path, include_broken: bool = False) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted((sample_dir / "sql").glob("*.sql")):
        files.append({"filename": path.name, "sql": path.read_text(encoding="utf-8")})
    if include_broken:
        for path in sorted((sample_dir / "sql_broken").glob("*.sql")):
            files.append({"filename": path.name, "sql": path.read_text(encoding="utf-8")})
    return files


def _decorate_cycles(cycles: list[dict], table_edges, column_edges) -> list[dict]:
    for cyc in cycles:
        nodes = set(cyc["nodes"])
        if cyc["level"] == "table":
            evidence = {
                f"{graph._f(e, 'script')}:{graph._f(e, 'line')}"
                for e in table_edges
                if graph._f(e, "source_table") in nodes and graph._f(e, "target_table") in nodes
            }
        else:
            evidence = {
                f"{graph._f(e, 'script')}:{graph._f(e, 'line')}"
                for e in column_edges
                if f"{graph._f(e, 'source_table')}.{graph._f(e, 'source_column')}" in nodes
                and f"{graph._f(e, 'target_table')}.{graph._f(e, 'target_column')}" in nodes
            }
        cyc["scripts"] = sorted(evidence)
    return cycles


def analyze(files: list[dict[str, str]], schemas: dict[str, list[str]]) -> dict:
    """纯计算：给定脚本与 schema，产出完整分析结果（不写库）。"""
    started = time.perf_counter()
    result = lineage.build_lineage(files, schemas)

    table_adj = graph.table_adjacency(result["table_edges"])
    column_adj = graph.column_adjacency(result["column_edges"])
    table_cycles = _decorate_cycles(
        graph.find_cycles(table_adj, "table"), result["table_edges"], result["column_edges"]
    )
    column_cycles = _decorate_cycles(
        graph.find_cycles(column_adj, "column"), result["table_edges"], result["column_edges"]
    )
    levels = graph.topo_levels(table_adj)

    analysis = {
        "scripts": result["scripts"],
        "table_edges": result["table_edges"],
        "column_edges": result["column_edges"],
        "diagnostics": result["diagnostics"],
        "parsed_count": result["parsed_count"],
        "failed_count": result["failed_count"],
        "table_levels": {k: v for k, v in sorted(levels.items(), key=lambda kv: (kv[1] is None, kv[1] or 0, kv[0]))},
        "cycles": table_cycles,
        "column_cycles": column_cycles,
        "target_kinds": result["target_kinds"],
        "catalog": result["catalog"],
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "note": None,
    }
    return analysis


def persist(conn, analysis: dict, source_label: str) -> str:
    return store.save_run(conn, analysis, source_label, duration_ms=analysis.get("duration_ms", 0))


def run_analysis(conn, files: list[dict[str, str]], schemas: dict[str, list[str]], source_label: str) -> tuple[str, dict]:
    analysis = analyze(files, schemas)
    run_id = persist(conn, analysis, source_label)
    return run_id, analysis


def run_sample(conn, sample_dir: Path, include_broken: bool = False) -> tuple[str, dict]:
    files = load_sample_files(sample_dir, include_broken)
    schemas = load_schemas(sample_dir)
    label = SAMPLE_WITH_BROKEN_LABEL if include_broken else SAMPLE_LABEL
    return run_analysis(conn, files, schemas, label)


def run_custom(conn, sql: str, filename: str, schemas: dict[str, list[str]]) -> tuple[str, dict]:
    files = [{"filename": filename or "自定义脚本.sql", "sql": sql}]
    return run_analysis(conn, files, schemas, f"自定义录入 · {files[0]['filename']}")


# --------------------------------------------------------------------------
# 整组变更演练（候选 schema 预演 + 幂等会话 + 两阶段切换）
# --------------------------------------------------------------------------

def _new_session_id(fingerprint: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"chg_{ts}_{fingerprint}"


def rehearse_changes(conn, raw_changes: list[dict], run_id: str) -> dict:
    """对一组变更做候选演练并落会话；同指纹直接复用，不重复生成方案。

    返回 {"session_id", "plan", "reused": bool}。
    """
    data = store.load_run(conn, run_id)
    if data is None:
        raise KeyError(f"找不到基线解析记录 {run_id}")

    base_schemas = store.load_schemas(conn)
    # 脚本产出对象的列也补进基线 schema，保证候选重放与基线口径一致
    for s in data["scripts"]:
        if s["status"] == "ok":
            base_schemas.setdefault(s["target"], s["columns"])

    sample_dir = settings.SAMPLE_DIR
    files = load_sample_files(sample_dir)
    # 以基线 run 实际录入的脚本为准（自定义 run 也能演练）
    run_files = [{"filename": s["filename"], "sql": s["sql"]} for s in data["scripts"]]
    if run_files:
        files = run_files

    chg = changes.changes_from_dicts(raw_changes)
    fingerprint = changes.changes_fingerprint(chg, run_id)

    existing = store.get_change_session_by_fingerprint(conn, fingerprint)
    if existing is not None:
        return {"session_id": existing["session_id"], "plan": existing["plan"], "reused": True}

    # 用基线 run 落库的边重建 analysis，避免重新解析带来口径漂移
    base_analysis = {
        "column_edges": data["column_edges"],
        "table_edges": data["table_edges"],
        "scripts": data["scripts"],
        "cycles": data["cycles"],
    }
    plan = changes.rehearse(chg, base_schemas, files, run_id, base_analysis)
    session_id = _new_session_id(fingerprint)
    store.save_change_session(conn, session_id, plan)
    return {"session_id": session_id, "plan": plan, "reused": False}


def decide_change_session(conn, session_id: str, approve: bool) -> dict:
    """两阶段切换：确认 → applied；拒绝或重放失败 → rejected 并保留原结构。"""
    record = store.get_change_session(conn, session_id)
    if record is None:
        raise KeyError(f"找不到演练会话 {session_id}")
    plan = record["plan"]
    updated = changes.apply_decision(plan, approve)
    store.update_change_session_plan(conn, session_id, updated)
    record = store.get_change_session(conn, session_id)
    return record  # type: ignore[return-value]
