"""依赖图算法：三色 DFS 环检测、拓扑层级、影响面（直接 / 间接）。

输入都是 lineage.build_lineage 产出的边列表，纯函数、无 IO，便于单测。
"""
from __future__ import annotations


def _f(edge, name):
    """边既可能是 dataclass（纯计算阶段），也可能是 sqlite3 行转换出的 dict。"""
    return edge[name] if isinstance(edge, dict) else getattr(edge, name)


def table_adjacency(table_edges) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in table_edges:
        adj.setdefault(_f(e, "source_table"), set()).add(_f(e, "target_table"))
    return adj


def column_adjacency(column_edges) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in column_edges:
        adj.setdefault(f"{_f(e, 'source_table')}.{_f(e, 'source_column')}", set()).add(
            f"{_f(e, 'target_table')}.{_f(e, 'target_column')}"
        )
    return adj


def all_nodes(adj: dict[str, set[str]]) -> list[str]:
    nodes = set(adj)
    for targets in adj.values():
        nodes |= set(targets)
    return sorted(nodes)


def find_cycles(adj: dict[str, set[str]], level: str = "table") -> list[dict]:
    """DFS 三色（白 0 / 灰 1 / 黑 2）：遇到指向灰色节点的边即为回边，输出环路径。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []
    cycles: dict[str, dict] = {}

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(adj.get(node, ())):
            state = color.get(nxt, WHITE)
            if state == WHITE:
                visit(nxt)
            elif state == GRAY:
                start = stack.index(nxt)
                path = stack[start:] + [nxt]
                body = path[:-1]
                pivot = min(range(len(body)), key=lambda i: body[i])
                canonical = body[pivot:] + body[:pivot]
                key = " -> ".join(canonical)
                cycles.setdefault(
                    key,
                    {"level": level, "path": path, "nodes": canonical, "key": key},
                )
        stack.pop()
        color[node] = BLACK

    for node in all_nodes(adj):
        if color.get(node, WHITE) == WHITE:
            visit(node)
    return list(cycles.values())


def column_key_to_table(node: str) -> str:
    return node.rsplit(".", 1)[0]


def _scc(adj: dict[str, set[str]], nodes: list[str]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    onstack: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        onstack.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in onstack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                onstack.discard(w)
                comp.append(w)
                if w == v:
                    break
            comps.append(comp)

    for node in nodes:
        if node not in index:
            strong(node)
    return comps


def topo_levels(adj: dict[str, set[str]]) -> dict[str, int | None]:
    """拓扑层级：无上游依赖为 0，其余 = 1 + 上游最大层级。

    环内节点（强连通分量 > 1 或自环）层级为 None（页面显示「环内」）。
    """
    nodes = all_nodes(adj)
    comps = _scc(adj, nodes)
    comp_of: dict[str, int] = {}
    for ci, comp in enumerate(comps):
        for n in comp:
            comp_of[n] = ci

    cyclic: set[int] = set()
    for ci, comp in enumerate(comps):
        if len(comp) > 1:
            cyclic.add(ci)
        elif comp[0] in adj.get(comp[0], ()):
            cyclic.add(ci)

    comp_edges: dict[int, set[int]] = {ci: set() for ci in range(len(comps))}
    for src, targets in adj.items():
        for tgt in targets:
            a, b = comp_of[src], comp_of[tgt]
            if a != b:
                comp_edges[a].add(b)

    preds: dict[int, set[int]] = {ci: set() for ci in comp_edges}
    for a, targets in comp_edges.items():
        for b in targets:
            preds[b].add(a)

    memo: dict[int, int | None] = {}

    def level_of(ci: int, guards: set[int] | None = None) -> int | None:
        if ci in memo:
            return memo[ci]
        if ci in cyclic:
            memo[ci] = None
            return None
        guards = guards or set()
        if ci in guards:
            return None
        guards.add(ci)
        pred_levels = [level_of(p, guards) for p in preds[ci]]
        if any(lv is None for lv in pred_levels):
            memo[ci] = None
            return None
        value = 0 if not pred_levels else 1 + max(pred_levels)
        memo[ci] = value
        return value

    return {n: level_of(comp_of[n]) for n in nodes}


def _bfs(adj: dict[str, set[str]], starts: list[str], max_depth: int = 10) -> dict[str, int]:
    dist: dict[str, int] = {s: 0 for s in starts}
    frontier = list(starts)
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for target in sorted(adj.get(node, ())):
                if target in dist:
                    continue
                if dist[node] + 1 > max_depth:
                    continue
                dist[target] = dist[node] + 1
                nxt.append(target)
        frontier = nxt
    return dist


def _level_sort_key(level: int | None) -> int:
    return 10_000 if level is None else level


def impact(
    table_edges,
    column_edges,
    levels: dict[str, int | None],
    table: str,
    column: str | None = None,
    max_depth: int = 10,
) -> dict:
    """输入表或列 → 下游对象清单（按拓扑层级排序，标注直接 / 间接）。"""
    target_kind = {}
    for e in table_edges:
        target_kind[_f(e, "target_table")] = _f(e, "target_kind")
    for e in column_edges:
        target_kind.setdefault(_f(e, "target_table"), _f(e, "target_kind"))

    column_adj = column_adjacency(column_edges)
    table_adj = table_adjacency(table_edges)
    seed_columns = sorted(
        {_f(e, "source_column") for e in column_edges if _f(e, "source_table") == table}
    )

    if column:
        start = f"{table}.{column}"
        if start not in column_adj and not any(
            _f(e, "source_table") == table and _f(e, "source_column") == column for e in column_edges
        ):
            return {
                "seed": start,
                "found": False,
                "downstream": [],
                "object_count": 0,
                "column_count": 0,
                "max_level": 0,
                "message": f"{table}.{column} 在血缘图中没有任何下游边（可能是叶子列或拼写有误）",
            }
        col_dist = _bfs(column_adj, [start], max_depth)
        objects: dict[str, dict] = {}
        for node, dist in col_dist.items():
            if node == start:
                continue
            obj = column_key_to_table(node)
            col = node.rsplit(".", 1)[1]
            entry = objects.setdefault(
                obj,
                {
                    "object": obj,
                    "kind": target_kind.get(obj, "unknown"),
                    "level": levels.get(obj),
                    "distance": dist,
                    "columns": [],
                },
            )
            entry["distance"] = min(entry["distance"], dist)
            entry["columns"].append(col)
        items = list(objects.values())
        column_count = len({n for n in col_dist if n != start})
    else:
        table_dist = _bfs(table_adj, [table], max_depth)
        col_dist: dict[str, int] = {}
        for seed_col in seed_columns:
            for node, dist in _bfs(column_adj, [f"{table}.{seed_col}"], max_depth).items():
                if node not in col_dist or dist < col_dist[node]:
                    col_dist[node] = dist
        objects = {}
        for obj, dist in table_dist.items():
            if obj == table:
                continue
            objects[obj] = {
                "object": obj,
                "kind": target_kind.get(obj, "unknown"),
                "level": levels.get(obj),
                "distance": dist,
                "columns": [],
            }
        for node in col_dist:
            obj = column_key_to_table(node)
            if obj in objects and obj != table:
                objects[obj]["columns"].append(node.rsplit(".", 1)[1])
        items = list(objects.values())
        column_count = len([n for n in col_dist if column_key_to_table(n) != table])

    for item in items:
        item["columns"] = sorted(set(item["columns"]))
        item["relation"] = "direct" if item["distance"] == 1 else "indirect"
        item["evidence"] = sorted(
            {
                f"{_f(e, 'script')}:{_f(e, 'line')}"
                for e in column_edges
                if _f(e, "target_table") == item["object"] and _f(e, "target_column") in item["columns"]
            }
        )
    items.sort(key=lambda it: (_level_sort_key(it["level"]), it["distance"], it["object"]))

    level_values = [it["level"] for it in items if it["level"] is not None]
    return {
        "seed": f"{table}.{column}" if column else table,
        "table": table,
        "column": column,
        "found": True,
        "downstream": items,
        "object_count": len(items),
        "column_count": column_count,
        "direct_count": sum(1 for it in items if it["relation"] == "direct"),
        "indirect_count": sum(1 for it in items if it["relation"] == "indirect"),
        "max_level": max(level_values) if level_values else 0,
        "seed_columns": seed_columns,
        "message": None,
    }
