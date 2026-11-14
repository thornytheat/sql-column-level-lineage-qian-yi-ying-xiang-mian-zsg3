"""整组源表变更演练：在候选 schema 上预演一组变更，逐层给出影响面与切换方案。

与单列 impact / migrate 的区别：
- 一次接受**一组**变更（加列 / 改名 / 改类型），而不是单条 drop/rename；
- 同时统计「明确引用」与「星号透传」——旧清单只看显式列引用，会漏掉
  SELECT * / t.* 自动带出新列、或经星号透传被改名的字段，导致误删仍有引用的列；
- 改名沿列级血缘**跨层传播**：上游列改名后，下游所有承接该列身份的列都要同步改；
- 产出「需重建视图 / 需回填物理表」两类动作，并在候选 schema 上重放全量脚本，
  用重放是否全绿决定能不能切换；失败则保留原结构（rejected），不产出可执行切换。

纯函数、无 IO，便于单测；持久化与状态机在 store / main 里。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from . import graph, lineage

# 变更动作枚举
ADD_COLUMN = "add_column"
RENAME_COLUMN = "rename_column"
ALTER_TYPE = "alter_type"
VALID_ACTIONS = (ADD_COLUMN, RENAME_COLUMN, ALTER_TYPE)

# 演练状态
DRAFT = "draft"          # 已生成方案，等待人工确认
CONFIRMED = "confirmed"  # 已确认，可切换
APPLIED = "applied"      # 切换成功（候选 schema 重放全绿）
REJECTED = "rejected"    # 重放失败，保留原结构
TERMINAL_STATES = (APPLIED, REJECTED)


class ChangeValidationError(ValueError):
    """变更组自身不合法（动作未知 / 表不存在 / 改名冲突等），在演练前拦截。"""


@dataclass
class Change:
    table: str
    action: str
    column: str | None = None          # rename / alter 的旧列名
    new_name: str | None = None        # rename 的新列名
    new_type: str | None = None        # add / alter 的类型
    comment: str | None = None

    def key(self) -> tuple:
        return (self.table, self.action, self.column or "", self.new_name or "", self.new_type or "")

    def ddl(self) -> str:
        if self.action == ADD_COLUMN:
            return f"ALTER TABLE {self.table} ADD COLUMN {self.new_name} {self.new_type or 'VARCHAR(64)'};"
        if self.action == RENAME_COLUMN:
            return f"ALTER TABLE {self.table} RENAME COLUMN {self.column} TO {self.new_name};"
        if self.action == ALTER_TYPE:
            return f"ALTER TABLE {self.table} ALTER COLUMN {self.column} TYPE {self.new_type};"
        raise ChangeValidationError(f"未知变更动作 {self.action}")

    def label(self) -> str:
        if self.action == ADD_COLUMN:
            return f"{self.table} 新增列 {self.new_name} {self.new_type or ''}".strip()
        if self.action == RENAME_COLUMN:
            return f"{self.table}.{self.column} 改名为 {self.new_name}"
        if self.action == ALTER_TYPE:
            return f"{self.table}.{self.column} 类型改为 {self.new_type}"
        return str(self.key())


def changes_fingerprint(changes: list[Change], base_run_id: str) -> str:
    """同一组变更 + 同一条基线 → 同一个指纹；重试直接复用方案，不重复生成。"""
    payload = {
        "base_run_id": base_run_id,
        "changes": [
            {
                "table": c.table,
                "action": c.action,
                "column": c.column,
                "new_name": c.new_name,
                "new_type": c.new_type,
            }
            for c in sorted(changes, key=lambda c: c.key())
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _validate(changes: list[Change], base_schemas: dict[str, list[str]]) -> None:
    if not changes:
        raise ChangeValidationError("变更组为空：至少需要一条 add_column / rename_column / alter_type")
    seen: set[tuple] = set()
    for c in changes:
        if c.action not in VALID_ACTIONS:
            raise ChangeValidationError(
                f"不支持的变更动作 {c.action!r}；可选 {', '.join(VALID_ACTIONS)}"
            )
        if c.table not in base_schemas:
            raise ChangeValidationError(f"变更目标表 {c.table} 不在基线 schema 中")
        if c.key() in seen:
            raise ChangeValidationError(f"变更组内存在重复条目：{c.label()}")
        seen.add(c.key())
        cols = base_schemas[c.table]
        if c.action == ADD_COLUMN:
            if not c.new_name:
                raise ChangeValidationError(f"{c.table} 加列缺少新列名 new_name")
            if c.new_name in cols:
                raise ChangeValidationError(f"{c.table} 已存在列 {c.new_name}，不能重复新增")
        elif c.action == RENAME_COLUMN:
            if c.column not in cols:
                raise ChangeValidationError(f"{c.table} 中不存在待改名列 {c.column}")
            if not c.new_name:
                raise ChangeValidationError(f"{c.table}.{c.column} 改名缺少新列名 new_name")
            if c.new_name in cols:
                raise ChangeValidationError(f"{c.table} 已存在列 {c.new_name}，改名会冲突")
        elif c.action == ALTER_TYPE:
            if c.column not in cols:
                raise ChangeValidationError(f"{c.table} 中不存在待改类型列 {c.column}")
            if not c.new_type:
                raise ChangeValidationError(f"{c.table}.{c.column} 改类型缺少 new_type")
    # 同表内改名/加列之间的新名字不能互相撞
    by_table: dict[str, list[Change]] = {}
    for c in changes:
        by_table.setdefault(c.table, []).append(c)
    for table, group in by_table.items():
        new_names = [c.new_name for c in group if c.action in (ADD_COLUMN, RENAME_COLUMN)]
        if len(new_names) != len(set(new_names)):
            raise ChangeValidationError(f"{table} 的变更里新列名重复：{new_names}")


def candidate_schemas(
    base_schemas: dict[str, list[str]], changes: list[Change]
) -> dict[str, list[str]]:
    """在基线 schema 上施加变更，得到候选表结构（不动入参）。"""
    candidate = {t: list(cols) for t, cols in base_schemas.items()}
    for c in changes:
        cols = candidate[c.table]
        if c.action == ADD_COLUMN:
            cols.append(c.new_name)  # type: ignore[arg-type]
        elif c.action == RENAME_COLUMN:
            candidate[c.table] = [c.new_name if x == c.column else x for x in cols]
        # alter_type 只改类型不改列名，列清单不变
    return candidate


def _affected_set(
    column_edges: list[dict],
    changes: list[Change],
    candidate_edges: list[dict] | None = None,
    seed_alias: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> dict[tuple[str, str], dict]:
    """沿列级血缘传播，找到每个受影响的「对象.列」，并记录命中方式与证据。

    返回 {(target_table, 源身份列名): 命中详情}；命中详情区分
    explicit（明确引用）与 star（星号透传），保留源列身份用于跨层改名，
    并同时给出基线列名 base_columns 与候选列名 candidate_columns。

    column_edges：基线边（旧列名 / 已有列）。
    candidate_edges：候选 schema 重放产生的边，覆盖改名后新列名、新增列经星号
      透传的场景，这些边在基线里还不存在，正是旧清单漏掉的部分。
    seed_alias：候选种子列 -> 基线源列身份，例如 (ods_products, product_sku)
      映射回 (ods_products, sku)，让改名后的边归并到同一变更。
    """
    seed_alias = seed_alias or {}

    def _identity(table: str, col: str) -> tuple[str, str]:
        return seed_alias.get((table, col), (table, col))

    # 种子身份：变更直接作用的源表列，统一用基线旧身份表示
    seed_origins: set[tuple[str, str]] = set()
    seed_via: dict[tuple[str, str], str] = {}
    for c in changes:
        if c.action == ADD_COLUMN:
            origin = (c.table, c.new_name)  # type: ignore[assignment]
        elif c.action == RENAME_COLUMN:
            origin = (c.table, c.column)
        else:  # ALTER_TYPE
            origin = (c.table, c.column)
        seed_origins.add(origin)
        seed_via[origin] = c.action

    base_out: dict[tuple[str, str], list[dict]] = {}
    for e in column_edges:
        base_out.setdefault((e["source_table"], e["source_column"]), []).append(e)
    cand_out: dict[tuple[str, str], list[dict]] = {}
    for e in candidate_edges or []:
        cand_out.setdefault((e["source_table"], e["source_column"]), []).append(e)

    affected: dict[tuple[str, str], dict] = {}

    def _walk(out: dict[tuple[str, str], list[dict]], is_candidate: bool) -> None:
        if is_candidate:
            starts = []
            for c in changes:
                if c.action == ADD_COLUMN:
                    starts.append((c.table, c.new_name))  # type: ignore[arg-type]
                elif c.action == RENAME_COLUMN:
                    starts.append((c.table, c.new_name))  # type: ignore[arg-type]
                else:
                    starts.append((c.table, c.column))
        else:
            starts = list(seed_origins)
        # 队列携带 (当前节点, 根种子身份)；跨层传播时根身份保持不变
        frontier = [(node, _identity(*node)) for node in starts]
        seen = set(starts)
        while frontier:
            node, root = frontier.pop()
            origin = _identity(*node)
            for e in out.get(node, ()):
                tgt_table, tgt_col = e["target_table"], e["target_column"]
                # 键用「对象 + 根身份列名」，同一源列跨层 / 改名新旧边都归并到同一条目
                key = (tgt_table, root[1])
                entry = affected.get(key)
                hit = "star" if e["star_expanded"] else "explicit"
                evidence = {"script": e["script"], "line": e["line"], "expr": e["expr"]}
                if entry is None:
                    entry = {
                        "target_table": tgt_table,
                        "target_column": tgt_col,
                        "target_kind": e["target_kind"],
                        "hits": [],
                        "evidence": [],
                        "origin_table": origin[0],
                        "origin_column": origin[1],
                        "root_table": root[0],
                        "root_column": root[1],
                        "via": seed_via.get(root, "propagate"),
                        "base_columns": set(),
                        "candidate_columns": set(),
                    }
                    affected[key] = entry
                if is_candidate:
                    entry["candidate_columns"].add(tgt_col)
                else:
                    entry["base_columns"].add(tgt_col)
                    entry["target_column"] = tgt_col
                if hit not in entry["hits"]:
                    entry["hits"].append(hit)
                if evidence not in entry["evidence"]:
                    entry["evidence"].append(evidence)
                nxt = (tgt_table, tgt_col)
                if nxt not in seen:
                    seen.add(nxt)
                    # 根身份继续向下传递
                    frontier.append((nxt, root))

    _walk(base_out, False)
    _walk(cand_out, True)

    for entry in affected.values():
        entry["base_columns"] = sorted(entry["base_columns"])
        entry["candidate_columns"] = sorted(entry["candidate_columns"] or entry["base_columns"])
    return affected


def _objects_by_level(
    affected: dict[tuple[str, str], dict],
    levels: dict[str, int | None],
    table_edges: list[dict],
) -> list[dict]:
    """把受影响列按对象聚合，带上拓扑层级、引用方式、重建/回填标记。"""
    kind_map = {e["target_table"]: e["target_kind"] for e in table_edges}
    grouped: dict[str, dict] = {}
    for (table, col), meta in sorted(affected.items()):
        obj = grouped.setdefault(
            table,
            {
                "object": table,
                "kind": kind_map.get(table, meta["target_kind"]),
                "level": levels.get(table),
                "columns": [],
                "column_details": [],
                "star_columns": [],
                "explicit_columns": [],
                "via": set(),
                "evidence": [],
            },
        )
        obj["columns"].append(col)
        obj["column_details"].append(
            {
                "base_column": meta["target_column"],
                "candidate_columns": meta["candidate_columns"],
                "origin": f"{meta['origin_table']}.{meta['origin_column']}",
                "root": f"{meta['root_table']}.{meta['root_column']}",
                "hits": meta["hits"],
            }
        )
        if "star" in meta["hits"]:
            obj["star_columns"].append(col)
        if "explicit" in meta["hits"]:
            obj["explicit_columns"].append(col)
        obj["via"].add(meta["via"])
        for ev in meta["evidence"]:
            if ev not in obj["evidence"]:
                obj["evidence"].append(ev)

    out = list(grouped.values())
    for o in out:
        o["columns"] = sorted(set(o["columns"]))
        o["column_details"].sort(key=lambda d: d["base_column"])
        o["star_columns"] = sorted(set(o["star_columns"]))
        o["explicit_columns"] = sorted(set(o["explicit_columns"]))
        o["via"] = sorted(o["via"])
        o["evidence"] = sorted(o["evidence"], key=lambda x: (x["script"], x["line"]))
        o["rebuild"] = o["kind"] == "view"        # 视图：重建定义
        o["backfill"] = o["kind"] == "table"      # 物理表：重跑回填
        o["relation"] = "direct" if o["level"] == 1 else ("indirect" if o["level"] else "cycle")
    out.sort(key=lambda o: (graph._level_sort_key(o["level"]), o["object"]))
    return out


def _replay(
    files: list[dict[str, str]], candidate: dict[str, list[str]]
) -> dict:
    """在候选 schema 上重放全量脚本，返回解析/血缘是否全绿、诊断与候选边。"""
    result = lineage.build_lineage(files, candidate)
    diagnostics = [
        {
            "filename": d.filename if hasattr(d, "filename") else d.get("filename"),
            "line": d.line if hasattr(d, "line") else d.get("line"),
            "col": d.col if hasattr(d, "col") else d.get("col"),
            "message": d.message if hasattr(d, "message") else d.get("message"),
            "code": d.code if hasattr(d, "code") else d.get("code"),
        }
        for d in result["diagnostics"]
    ]
    failed_scripts = [s for s in result["scripts"] if s["status"] == "error"]
    return {
        "parsed_count": result["parsed_count"],
        "failed_count": result["failed_count"],
        "total_count": len(result["scripts"]),
        "diagnostics": diagnostics,
        "failed_scripts": [
            {"filename": s["filename"], "message": s["message"], "line": s.get("error_line"), "col": s.get("error_col")}
            for s in failed_scripts
        ],
        "ok": result["failed_count"] == 0,
        "catalog": result["catalog"],
        "column_edges": result["column_edges"],
        "table_edges": result["table_edges"],
        "column_edge_count": len(result["column_edges"]),
        "table_edge_count": len(result["table_edges"]),
    }


def rehearse(
    changes: list[Change],
    base_schemas: dict[str, list[str]],
    files: list[dict[str, str]],
    base_run_id: str,
    base_analysis: dict,
) -> dict:
    """演练一组变更：校验 → 候选 schema → 传播影响 → 分层对象 → 候选重放。

    不修改 base_schemas / base_analysis；返回可直接持久化的方案 dict。
    """
    _validate(changes, base_schemas)
    candidate = candidate_schemas(base_schemas, changes)

    column_edges = base_analysis["column_edges"]
    table_edges = base_analysis["table_edges"]
    levels = graph.topo_levels(graph.table_adjacency(table_edges))

    # 先在候选 schema 上重放：拿到改名后新列名 / 新增列经星号透传产生的边
    replay = _replay(files, candidate)
    candidate_edges = replay["column_edges"]

    # 候选新列名 -> 基线旧列身份的别名，仅用于改名归并
    seed_alias: dict[tuple[str, str], tuple[str, str]] = {}
    for c in changes:
        if c.action == RENAME_COLUMN:
            seed_alias[(c.table, c.new_name)] = (c.table, c.column)  # type: ignore[assignment]

    affected = _affected_set(
        column_edges, changes, candidate_edges=candidate_edges, seed_alias=seed_alias
    )
    objects = _objects_by_level(affected, levels, table_edges)

    # 分层视图：明确引用层 / 星号透传层（一个对象可能两者都占）
    explicit_objects = [o for o in objects if o["explicit_columns"]]
    star_objects = [o for o in objects if o["star_columns"]]
    rebuild_views = [o["object"] for o in objects if o["rebuild"]]
    backfill_tables = [o["object"] for o in objects if o["backfill"]]

    def _chain_for(origin_table: str, origin_col: str) -> list[dict]:
        chain = []
        for (table, _col), meta in sorted(affected.items()):
            # 按根种子身份归组，跨层对象（直接上游已不是源表）也能纳入同一条传播链
            if (meta["root_table"], meta["root_column"]) != (origin_table, origin_col):
                continue
            base_cols = meta["base_columns"]
            cand_cols = meta["candidate_columns"]
            chain.append(
                {
                    "object": table,
                    "level": levels.get(table),
                    "base_columns": base_cols,
                    "candidate_columns": cand_cols,
                    # 加列在基线里没有旧列（base_cols 为空），不算改名，只算新增透传
                    "renamed": bool(base_cols) and base_cols != cand_cols,
                    "newly_added": not bool(base_cols) and bool(cand_cols),
                    "hits": meta["hits"],
                    "evidence": meta["evidence"],
                }
            )
        return chain

    # 改名跨层映射：源表旧列 → 每个下游对象，基线列名与候选新列名并列
    rename_propagation: list[dict] = []
    for c in changes:
        if c.action != RENAME_COLUMN:
            continue
        rename_propagation.append(
            {
                "table": c.table,
                "from_column": c.column,
                "to_column": c.new_name,
                "downstream_chain": _chain_for(c.table, c.column),
            }
        )

    # 加列透传：新列经星号自动进入哪些对象（旧清单最容易漏的部分）
    add_propagation: list[dict] = []
    for c in changes:
        if c.action != ADD_COLUMN:
            continue
        add_propagation.append(
            {
                "table": c.table,
                "new_column": c.new_name,
                "new_type": c.new_type,
                "downstream_chain": _chain_for(c.table, c.new_name),  # type: ignore[arg-type]
            }
        )

    # 类型变更：只影响「承接该列身份」的对象，标注是否被 CAST/ROUND 收敛
    type_propagation: list[dict] = []
    for c in changes:
        if c.action != ALTER_TYPE:
            continue
        chain = []
        for item in _chain_for(c.table, c.column):
            table = item["object"]
            # 收集该对象受影响列上的全部入边变换链（不限直接源表，跨层也能看到历史 CAST/ROUND）
            transforms = sorted(
                {
                    tuple(e["transforms"])
                    for e in column_edges
                    if e["target_table"] == table and e["target_column"] in item["base_columns"]
                }
            )
            item["transforms"] = [list(t) for t in transforms if t]
            chain.append(item)
        type_propagation.append(
            {"table": c.table, "column": c.column, "new_type": c.new_type, "downstream_chain": chain}
        )

    steps = _ordered_steps(changes, objects, levels, replay)

    # 候选 schema 只保留真实输入表（脚本产出的视图/表由重放 catalog 体现，避免 None 列）
    candidate_schemas_out = {
        t: list(cols) for t, cols in sorted(candidate.items()) if t in base_schemas
    }
    candidate_catalog = {
        t: list(cols) for t, cols in sorted(replay["catalog"].items()) if cols is not None
    }

    payload = {
        "status": DRAFT,
        "fingerprint": changes_fingerprint(changes, base_run_id),
        "base_run_id": base_run_id,
        "changes": [_change_dict(c) for c in changes],
        "change_labels": [c.label() for c in changes],
        "ddl_preview": [c.ddl() for c in changes],
        "base_schemas": {t: list(cols) for t, cols in sorted(base_schemas.items())},
        "candidate_schemas": candidate_schemas_out,
        "candidate_catalog": candidate_catalog,
        "affected_objects": objects,
        "affected_object_count": len(objects),
        "explicit_objects": explicit_objects,
        "star_objects": star_objects,
        "rebuild_views": rebuild_views,
        "backfill_tables": backfill_tables,
        "rename_propagation": rename_propagation,
        "add_propagation": add_propagation,
        "type_propagation": type_propagation,
        "replay": replay,
        "steps": steps,
        "step_count": len(steps),
        "ready": replay["ok"],
        "warnings": _warnings(changes, objects, replay),
    }
    return payload


def _change_dict(c: Change) -> dict:
    return {
        "table": c.table,
        "action": c.action,
        "column": c.column,
        "new_name": c.new_name,
        "new_type": c.new_type,
        "comment": c.comment,
    }


def changes_from_dicts(raw: list[dict]) -> list[Change]:
    out: list[Change] = []
    for item in raw or []:
        out.append(
            Change(
                table=str(item.get("table") or ""),
                action=str(item.get("action") or ""),
                column=(str(item["column"]) if item.get("column") not in (None, "") else None),
                new_name=(str(item["new_name"]) if item.get("new_name") not in (None, "") else None),
                new_type=(str(item["new_type"]) if item.get("new_type") not in (None, "") else None),
                comment=(str(item["comment"]) if item.get("comment") not in (None, "") else None),
            )
        )
    return out


def _ordered_steps(changes: list[Change], objects: list[dict], levels: dict, replay: dict) -> list[dict]:
    """切换顺序：冻结 → 逐层改下游（先浅后深）→ 回填物理表 → 源表 DDL → 候选重放复核。"""
    steps: list[dict] = []
    steps.append(
        {
            "order": 1,
            "action": "freeze",
            "title": "冻结下游写入并记录基线",
            "detail": f"暂停 {len(objects)} 个受影响对象的调度，记录行数与校验和；源表结构保持不变，可随时回退。",
            "objects": [],
        }
    )
    level_values = sorted({o["level"] for o in objects if o["level"] is not None})
    for lv in level_values:
        objs = [o for o in objects if o["level"] == lv]
        steps.append(
            {
                "order": len(steps) + 1,
                "action": "rewrite",
                "title": f"改写/重建第 {lv} 层对象（{len(objs)} 个）",
                "detail": (
                    "明确引用按新列名/新类型改写；星号透传对象无需改 SELECT 列表，但要重建定义以拉平新列。"
                    "本层只依赖更上游，改完可单独重跑验证。"
                ),
                "objects": objs,
            }
        )
    cycle_objs = [o for o in objects if o["level"] is None]
    if cycle_objs:
        steps.append(
            {
                "order": len(steps) + 1,
                "action": "uncycle",
                "title": f"先断开环内对象（{len(cycle_objs)} 个）",
                "detail": "环内对象拓扑层级不确定，需先确认环朝向或临时物化后再继续。",
                "objects": cycle_objs,
            }
        )
    backfill = [o for o in objects if o["backfill"]]
    if backfill:
        steps.append(
            {
                "order": len(steps) + 1,
                "action": "backfill",
                "title": f"回填物理表（{len(backfill)} 个）",
                "detail": "CTAS / INSERT 产出的物理表不会自动跟随源列变化，按层级从浅到深重跑回填；视图不在此列。",
                "objects": backfill,
            }
        )
    steps.append(
        {
            "order": len(steps) + 1,
        "action": "ddl",
        "title": f"在源表执行 {len(changes)} 条 DDL 并复核",
        "detail": "下游全部改写完成后再执行源表 DDL；执行后用候选 schema 重放全量脚本，确认 0 失败。",
        "objects": [],
        "ddl": [c.ddl() for c in changes],
        }
    )
    steps.append(
        {
            "order": len(steps) + 1,
            "action": "verify",
            "title": "候选 schema 全量重放复核",
            "detail": (
                f"重放 {replay['total_count']} 个脚本：成功 {replay['parsed_count']} / 失败 {replay['failed_count']}。"
                + ("全部通过，允许切换。" if replay["ok"] else "存在失败，保留原结构，禁止切换。")
            ),
            "objects": [],
            "replay_ok": replay["ok"],
        }
    )
    return steps


def _warnings(changes: list[Change], objects: list[dict], replay: dict) -> list[str]:
    warnings: list[str] = []
    star = [o["object"] for o in objects if o["star_columns"]]
    if star:
        warnings.append(
            "存在星号透传对象（" + "、".join(star) + "）：SELECT * / t.* 会自动带出新增列与改名列，"
            "旧的仅显式引用清单会漏掉它们，禁止按旧清单删列。"
        )
    renames = [c for c in changes if c.action == RENAME_COLUMN]
    if renames:
        warnings.append(
            f"含 {len(renames)} 条改名：新列名沿列级血缘跨层传播，需逐层同步下游引用，"
            "不能只改源表 DDL。"
        )
    alters = [c for c in changes if c.action == ALTER_TYPE]
    if alters:
        warnings.append(
            "类型变更（" + "、".join(f"{c.table}.{c.column}→{c.new_type}" for c in alters) +
            "）需确认下游 CAST/ROUND 口径与目标精度兼容，物理表要按新精度回填。"
        )
    if not replay["ok"]:
        warnings.append(
            f"候选 schema 重放有 {replay['failed_count']} 个脚本失败，方案处于不可切换状态；"
            "失败时保留原结构，修复后用同一组变更重试（指纹不变，复用本方案）。"
        )
    return warnings


def apply_decision(plan: dict, approve: bool) -> dict:
    """模拟切换决策：只在重放全绿且人工确认后才标记 applied；否则 rejected 并保留原结构。

    返回深拷贝后的新方案 dict，不就地修改入参。
    """
    out = copy.deepcopy(plan)
    if out["status"] in TERMINAL_STATES:
        return out
    if not approve:
        out["status"] = REJECTED
        out["ready"] = False
        out["rollback"] = "已保留原结构：未执行任何源表 DDL，基线 schema 与脚本维持不变。"
        return out
    if not out["ready"] or not out["replay"]["ok"]:
        out["status"] = REJECTED
        out["ready"] = False
        out["rollback"] = "候选重放未全绿，拒绝切换并保留原结构；待脚本修复后重试。"
        return out
    out["status"] = APPLIED
    out["rollback"] = ""
    return out
