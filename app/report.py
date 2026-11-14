"""结果视图模型：分组列级关系、诊断片段、导出。

页面不直接甩 JSON，这里把库里的边整理成「按目标表分组的列 ← 源列」结构，
每条边带 脚本:行号 + 表达式 + 变换链，作为结论的直接依据。
"""
from __future__ import annotations

import csv
import io
import json


def snippet(sql: str, line: int, window: int = 2) -> dict:
    lines = sql.splitlines()
    if not lines:
        return {"lines": [], "caret_col": 1}
    hit = max(1, min(line or 1, len(lines)))
    start = max(1, hit - window)
    end = min(len(lines), hit + window)
    return {
        "lines": [
            {"no": no, "text": lines[no - 1], "hit": no == hit} for no in range(start, end + 1)
        ],
        "caret_col": 1,
    }


def failed_scripts(scripts: list[dict]) -> list[dict]:
    out = []
    for s in scripts:
        if s["status"] != "error":
            continue
        out.append(
            {
                "filename": s["filename"],
                "target": s["target"],
                "line": s.get("error_line"),
                "col": s.get("error_col"),
                "message": s.get("message") or "解析失败（无详细信息）",
                "snippet": snippet(s["sql"], s.get("error_line") or 1),
            }
        )
    return out


def grouped_columns(column_edges: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for e in column_edges:
        group = groups.setdefault(
            e["target_table"],
            {
                "target_table": e["target_table"],
                "kind": e["target_kind"],
                "edge_count": 0,
                "columns": {},
            },
        )
        group["edge_count"] += 1
        col = group["columns"].setdefault(
            e["target_column"],
            {"target_column": e["target_column"], "sources": []},
        )
        col["sources"].append(
            {
                "source": f"{e['source_table']}.{e['source_column']}",
                "source_table": e["source_table"],
                "source_column": e["source_column"],
                "expr": e["expr"],
                "transforms": e["transforms"],
                "transform_chain": " → ".join(list(e["transforms"]) + [e["source_column"]])
                if e["transforms"]
                else e["source_column"],
                "star": e["star_expanded"],
                "union_branch": e["union_branch"],
                "evidence": f"{e['script']}:{e['line']}",
                "script": e["script"],
                "line": e["line"],
            }
        )
    result = []
    for group in groups.values():
        columns = []
        for col in group["columns"].values():
            col["sources"].sort(key=lambda s: (s["script"], s["line"], s["source"]))
            columns.append(col)
        columns.sort(key=lambda c: c["target_column"])
        group["columns"] = columns
        group["column_count"] = len(columns)
        result.append(group)
    result.sort(key=lambda g: g["target_table"])
    return result


def export_payload(run: dict, scripts: list[dict], table_edges: list[dict], column_edges: list[dict], cycles: list[dict]) -> dict:
    return {
        "app": "lineagex",
        "run": run,
        "scripts": [
            {
                "filename": s["filename"],
                "kind": s["kind"],
                "target": s["target"],
                "status": s["status"],
                "columns": s["columns"],
                "error": (
                    {"line": s.get("error_line"), "col": s.get("error_col"), "message": s.get("message")}
                    if s["status"] == "error"
                    else None
                ),
            }
            for s in scripts
        ],
        "table_edges": table_edges,
        "column_edges": column_edges,
        "cycles": cycles,
    }


def export_csv(column_edges: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "expr",
            "transforms",
            "star_expanded",
            "union_branch",
            "target_kind",
            "script",
            "line",
        ]
    )
    for e in column_edges:
        writer.writerow(
            [
                e["source_table"],
                e["source_column"],
                e["target_table"],
                e["target_column"],
                e["expr"],
                "|".join(e["transforms"]),
                1 if e["star_expanded"] else 0,
                e["union_branch"],
                e["target_kind"],
                e["script"],
                e["line"],
            ]
        )
    return buffer.getvalue()


def catalog_columns(catalog: dict, table: str) -> list[str]:
    return catalog.get(table, [])


def dump_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
