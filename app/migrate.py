"""迁移清单生成：删列 / 改名时，按拓扑层级给出有序步骤。

顺序原则（先改下游、再动源表）：
1. 冻结写入 + 记录基线；
2. 按依赖层级从浅到深改写视图 / 任务里的列引用；
3. 回填需重算的物理表（视图随定义自动生效，只标注重建）；
4. 最后才在源表执行 DROP / RENAME；
每一步都带对象清单与证据（脚本:行号），便于派工。
"""
from __future__ import annotations


def _ddl_sql(table: str, column: str, operation: str, new_name: str | None) -> str:
    if operation == "rename":
        return f"ALTER TABLE {table} RENAME COLUMN {column} TO {new_name};"
    return f"ALTER TABLE {table} DROP COLUMN {column};"


def plan_migration(
    impact_data: dict,
    operation: str,
    new_name: str | None = None,
    cycles: list[dict] | None = None,
) -> dict:
    table = impact_data.get("table") or ""
    column = impact_data.get("column") or ""
    items = impact_data.get("downstream", [])
    cycles = cycles or []

    steps: list[dict] = []
    steps.append(
        {
            "order": 1,
            "action": "freeze",
            "title": "冻结下游写入并记录基线",
            "detail": (
                f"暂停写入 {table} 的调度任务，记录 {len(items)} 个下游对象的行数与校验和；"
                "此时源列仍然存在，改动可随时回退。"
            ),
            "objects": [],
        }
    )

    levels = sorted({it["level"] for it in items if it["level"] is not None})
    for level in levels:
        objs = [it for it in items if it["level"] == level]
        steps.append(
            {
                "order": len(steps) + 1,
                "action": "rewrite",
                "title": f"改写第 {level} 层下游对象（{len(objs)} 个）",
                "detail": (
                    f"把 {table}.{column} 的引用替换为 "
                    + (
                        f"{table}.{new_name}（改名后新列名）"
                        if operation == "rename" and new_name
                        else "新的来源列或常量"
                    )
                    + "；本层对象只依赖第 "
                    + str(max(level - 1, 0))
                    + " 层及更上游，改写后可单独重跑验证。"
                ),
                "objects": objs,
            }
        )

    cycle_items = [it for it in items if it["level"] is None]
    if cycle_items:
        steps.append(
            {
                "order": len(steps) + 1,
                "action": "uncycle",
                "title": f"先断开环内下游对象（{len(cycle_items)} 个）",
                "detail": "这些对象位于依赖环中，拓扑层级不确定；需先确认环的合法朝向或临时物化，再继续迁移。",
                "objects": cycle_items,
            }
        )

    backfill = [it for it in items if it["kind"] == "table"]
    steps.append(
        {
            "order": len(steps) + 1,
            "action": "backfill",
            "title": f"回填需重算的物理表（{len(backfill)} 个）",
            "detail": (
                "物理表（CTAS / INSERT SELECT 产出）不会自动跟随源列变化，需要按依赖层级从浅到深重跑回填；"
                "视图只需重建定义或重新解析，不在此列。"
            ),
            "objects": backfill,
        }
    )

    steps.append(
        {
            "order": len(steps) + 1,
            "action": "ddl",
            "title": f"在源表执行{'删列' if operation != 'rename' else '改名'}并复核",
            "detail": f"确认下游全部改写完成后再执行：{_ddl_sql(table, column, operation, new_name)} 执行后重新解析全部脚本，确认 0 条引用了旧列。",
            "objects": [],
        }
    )

    warnings: list[str] = []
    if not items:
        warnings.append("该列没有下游引用，可直接执行 DDL；仍建议保留一次全量脚本解析作为复核。")
    if any(it["kind"] == "view" for it in items):
        warnings.append("受影响对象中包含视图：视图随定义自动生效，但需要重新解析并部署新定义，否则查询会报旧列不存在。")
    if cycles:
        warnings.append(f"当前血缘图存在 {len(cycles)} 条环，迁移前建议先处理：" + "；".join(c["key"] for c in cycles[:3]))
    if operation == "rename" and not new_name:
        warnings.append("改名操作缺少新列名，DDL 步骤不可直接执行。")

    return {
        "table": table,
        "column": column,
        "operation": operation,
        "new_name": new_name,
        "ddl_sql": _ddl_sql(table, column, operation, new_name) if not (operation == "rename" and not new_name) else "",
        "steps": steps,
        "step_count": len(steps),
        "affected_objects": len(items),
        "affected_columns": impact_data.get("column_count", 0),
        "backfill_count": len(backfill),
        "direct_count": impact_data.get("direct_count", 0),
        "indirect_count": impact_data.get("indirect_count", 0),
        "warnings": warnings,
    }
