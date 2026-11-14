"""列级血缘构建：把解析后的语句落成「源列 → 目标列」的边。

规则要点：
- 目标列名优先取 AS 别名 / 隐式别名，其次取单列名，函数包裹取 函数_源列；
- SELECT * / t.* 按 schema 展开成全部源列，并打 star_expanded 标记；
- 函数包裹（CAST/COALESCE/ROUND/SUM...）保留源列身份，记录由外到内的变换链；
- 子查询先算内部输出列，再按列名向外传播（不丢源表）；
- UNION ALL 各分支按列序号对齐到同一目标列；
- 来源表既不在 schema、也不是本批次脚本产出 → 报错并给位置，不静默丢边。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import parser


@dataclass
class Diagnostic:
    filename: str
    line: int
    col: int
    message: str
    code: str = "parse_error"
    severity: str = "error"


@dataclass
class Ref:
    table: str
    column: str
    transforms: tuple[str, ...] = ()
    expr: str = ""
    line: int = 0
    star: bool = False
    branch: int = 0


@dataclass
class OutCol:
    name: str
    refs: list[Ref] = field(default_factory=list)


@dataclass
class ColumnEdge:
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    script: str
    line: int
    expr: str
    transforms: list[str]
    star_expanded: bool
    union_branch: int
    target_kind: str


@dataclass
class TableEdge:
    source_table: str
    target_table: str
    script: str
    line: int
    target_kind: str


@dataclass
class ScriptResult:
    filename: str
    kind: str
    target: str
    status: str  # ok | error
    columns: list[str]
    line: int
    message: str | None = None
    error_line: int | None = None
    error_col: int | None = None
    sql: str = ""


class _LineageError(Exception):
    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col


class _UnknownTable(_LineageError):
    pass


@dataclass
class _Source:
    name: str
    table: str | None
    cols: list[str]
    outputs: list[OutCol] | None

    def has_column(self, name: str) -> bool:
        if self.outputs is not None:
            return any(c.name == name for c in self.outputs)
        return name in self.cols


def _kind_of(stmt: parser.Statement) -> str:
    return "view" if stmt.kind == "view" else "table"


def _sources(select: parser.Select, catalog: dict[str, list[str]]) -> list[_Source]:
    out: list[_Source] = []
    for idx, ref in enumerate(select.froms):
        if ref.subquery is not None:
            inner = _resolve_select(ref.subquery, catalog)
            out.append(
                _Source(
                    name=ref.alias or ref.table or f"sub_{idx + 1}",
                    table=None,
                    cols=[],
                    outputs=inner,
                )
            )
        else:
            cols = catalog.get(ref.table)
            if cols is None:
                raise _UnknownTable(
                    f"未知来源表 {ref.table}：既不在 schema 清单中，也不是本批次脚本产出的表",
                    ref.line,
                    ref.col,
                )
            out.append(_Source(name=ref.alias or ref.table, table=ref.table, cols=cols, outputs=None))
    return out


def _match_source(sources: list[_Source], qualifier: str | None, column: str, line: int, col: int) -> _Source:
    if qualifier:
        cands = [
            s
            for s in sources
            if s.name == qualifier or (s.table or "").split(".")[-1] == qualifier
        ]
        if not cands:
            raise _LineageError(
                f"表别名 {qualifier} 不在 FROM 子句中（可用别名：{', '.join(s.name for s in sources)}）",
                line,
                col,
            )
        if not cands[0].has_column(column):
            raise _LineageError(f"表 {cands[0].name} 中不存在列 {column}", line, col)
        return cands[0]
    cands = [s for s in sources if s.has_column(column)]
    if not cands:
        raise _LineageError(f"列 {column} 不属于任何来源表", line, col)
    if len(cands) > 1:
        raise _LineageError(
            f"列 {column} 在多个来源表中存在（{', '.join(s.name for s in cands)}），请加表别名",
            line,
            col,
        )
    return cands[0]


def _resolve_refs(item: parser.SelectItem, sources: list[_Source]) -> list[Ref]:
    refs: list[Ref] = []
    for cr in item.refs:
        src = _match_source(sources, cr.qualifier, cr.column, cr.line, cr.col)
        if src.outputs is not None:
            inner = next(c for c in src.outputs if c.name == cr.column)
            if not inner.refs:
                raise _LineageError(f"子查询列 {cr.column} 没有可追溯的源列", cr.line, cr.col)
            for r in inner.refs:
                refs.append(
                    Ref(
                        table=r.table,
                        column=r.column,
                        transforms=tuple(item.transforms) + r.transforms,
                        expr=item.expr,
                        line=item.line,
                        star=r.star,
                        branch=r.branch,
                    )
                )
        else:
            refs.append(
                Ref(
                    table=src.table or src.name,
                    column=cr.column,
                    transforms=tuple(item.transforms),
                    expr=item.expr,
                    line=item.line,
                )
            )
    return refs


def _output_name(item: parser.SelectItem, refs: list[Ref], index: int) -> str:
    if item.alias:
        return item.alias
    if len(item.refs) == 1:
        return item.refs[0].column
    if len(refs) == 1 and item.transforms:
        return f"{item.transforms[-1].lower()}_{refs[0].column}"
    return f"col_{index + 1}"


def _expand_star(
    item: parser.SelectItem,
    sources: list[_Source],
    catalog: dict[str, list[str]],
) -> list[OutCol]:
    targets = sources
    if item.star_qualifier:
        targets = [
            s
            for s in sources
            if s.name == item.star_qualifier
            or (s.table or "").split(".")[-1] == item.star_qualifier
        ]
        if not targets:
            raise _LineageError(
                f"SELECT {item.star_qualifier}.* 中的别名不在 FROM 子句里",
                item.line,
                item.col,
            )
    out: list[OutCol] = []
    for src in targets:
        if src.outputs is not None:
            for col in src.outputs:
                out.append(
                    OutCol(
                        name=col.name,
                        refs=[
                            Ref(
                                table=r.table,
                                column=r.column,
                                transforms=r.transforms,
                                expr=item.expr,
                                line=item.line,
                                star=True,
                                branch=r.branch,
                            )
                            for r in col.refs
                        ],
                    )
                )
        else:
            cols = catalog.get(src.table or "")
            if cols is None:
                raise _UnknownTable(f"未知来源表 {src.name}", item.line, item.col)
            for name in cols:
                out.append(
                    OutCol(
                        name=name,
                        refs=[
                            Ref(
                                table=src.table or src.name,
                                column=name,
                                expr=item.expr,
                                line=item.line,
                                star=True,
                            )
                        ],
                    )
                )
    if not out:
        raise _LineageError("SELECT * 没有可展开的来源列", item.line, item.col)
    return out


def _resolve_select(select: parser.Select, catalog: dict[str, list[str]]) -> list[OutCol]:
    sources = _sources(select, catalog)
    cols: list[OutCol] = []
    for idx, item in enumerate(select.items):
        if item.star:
            cols.extend(_expand_star(item, sources, catalog))
            continue
        refs = _resolve_refs(item, sources)
        cols.append(OutCol(name=_output_name(item, refs, idx), refs=refs))

    for bi, branch in enumerate(select.unions, start=1):
        branch_cols = _resolve_select(branch, catalog)
        if len(branch_cols) != len(cols):
            raise _LineageError(
                f"UNION 分支列数不一致：第 1 个分支 {len(cols)} 列，第 {bi + 1} 个分支 {len(branch_cols)} 列",
                branch.line,
                1,
            )
        for target, bcol in zip(cols, branch_cols):
            for r in bcol.refs:
                target.refs.append(
                    Ref(
                        table=r.table,
                        column=r.column,
                        transforms=r.transforms,
                        expr=r.expr,
                        line=r.line,
                        star=r.star,
                        branch=bi,
                    )
                )
    return cols


def _resolve_statement(stmt: parser.Statement, catalog: dict[str, list[str]]) -> list[OutCol]:
    cols = _resolve_select(stmt.select, catalog)
    if stmt.target_columns:
        if len(stmt.target_columns) != len(cols):
            raise _LineageError(
                f"目标列清单 {len(stmt.target_columns)} 列，SELECT 输出 {len(cols)} 列，数量不一致",
                stmt.line,
                1,
            )
        cols = [
            OutCol(name=stmt.target_columns[i], refs=cols[i].refs) for i in range(len(cols))
        ]
    return cols


def _collect_base_tables(select: parser.Select) -> list[parser.TableRef]:
    found: list[parser.TableRef] = []
    for ref in select.froms:
        if ref.subquery is not None:
            found.extend(_collect_base_tables(ref.subquery))
        else:
            found.append(ref)
    for branch in select.unions:
        found.extend(_collect_base_tables(branch))
    return found


def build_lineage(
    files: list[dict[str, str]],
    schemas: dict[str, list[str]],
) -> dict:
    """files: [{"filename":..., "sql":...}]；schemas: {表名: [列名]}。"""
    catalog: dict[str, list[str]] = {name: list(cols) for name, cols in schemas.items()}
    results: list[ScriptResult] = []
    statements: list[tuple[int, parser.Statement]] = []
    target_kind: dict[str, str] = {}

    # 1) 解析
    for idx, item in enumerate(files):
        filename = item["filename"]
        sql = item.get("sql", "")
        try:
            stmt = parser.parse_script(sql, filename)
        except parser.ParseError as exc:
            results.append(
                ScriptResult(
                    filename=filename,
                    kind="-",
                    target="-",
                    status="error",
                    columns=[],
                    line=exc.line,
                    message=exc.message,
                    error_line=exc.line,
                    error_col=exc.col,
                    sql=sql,
                )
            )
            continue
        statements.append((idx, stmt))
        target_kind[stmt.target] = _kind_of(stmt)
        results.append(
            ScriptResult(
                filename=filename,
                kind=stmt.kind,
                target=stmt.target,
                status="pending",
                columns=[],
                line=stmt.line,
                sql=sql,
            )
        )

    # 2) 预登记目标列名（别名/列名可静态推导），让后置视图能被前向引用解析
    for _, stmt in statements:
        names = list(stmt.target_columns) if stmt.target_columns else [
            n for n in parser.output_names(stmt.select) if n
        ]
        catalog.setdefault(stmt.target, names)

    # 3) 逐条解析血缘，未知表留到下一轮（最多 3 轮）
    resolved: dict[str, list[OutCol]] = {}
    errors: dict[str, Diagnostic] = {}
    unknown: dict[str, Diagnostic] = {}
    pending = [stmt for _, stmt in statements]
    for _ in range(3):
        progressed = False
        for stmt in list(pending):
            try:
                cols = _resolve_statement(stmt, catalog)
            except _UnknownTable as exc:
                unknown[stmt.filename] = Diagnostic(
                    filename=stmt.filename,
                    line=exc.line,
                    col=exc.col,
                    message=exc.message,
                    code="unknown_table",
                )
                continue
            except _LineageError as exc:
                errors[stmt.filename] = Diagnostic(
                    filename=stmt.filename,
                    line=exc.line,
                    col=exc.col,
                    message=exc.message,
                    code="lineage_error",
                )
                pending.remove(stmt)
                progressed = True
                resolved[stmt.filename] = []
                continue
            resolved[stmt.filename] = cols
            catalog[stmt.target] = [c.name for c in cols]
            pending.remove(stmt)
            progressed = True
        if not pending or not progressed:
            break
    for stmt in pending:
        if stmt.filename in unknown:
            errors[stmt.filename] = unknown[stmt.filename]
            resolved[stmt.filename] = []
            continue
        errors[stmt.filename] = Diagnostic(
            filename=stmt.filename,
            line=stmt.line,
            col=1,
            message="来源表无法解析：引用的表既不在 schema 中，也不是本批次脚本产出的表",
            code="unknown_table",
        )
        resolved[stmt.filename] = []

    # 4) 生成边与状态
    table_edges: list[TableEdge] = []
    column_edges: list[ColumnEdge] = []
    for result in results:
        if result.status == "error":
            continue
        stmt = next((s for _, s in statements if s.filename == result.filename), None)
        if stmt is None:
            continue
        diag = errors.get(result.filename)
        cols = resolved.get(result.filename, [])
        if diag is not None:
            result.status = "error"
            result.message = diag.message
            result.error_line = diag.line
            result.error_col = diag.col
            continue
        result.status = "ok"
        result.columns = [c.name for c in cols]
        kind = _kind_of(stmt)
        seen: set[tuple[str, int]] = set()
        for ref in _collect_base_tables(stmt.select):
            key = (ref.table or "", ref.line)
            if key in seen:
                continue
            seen.add(key)
            table_edges.append(
                TableEdge(
                    source_table=ref.table or "",
                    target_table=stmt.target,
                    script=result.filename,
                    line=ref.line,
                    target_kind=kind,
                )
            )
        for col in cols:
            for r in col.refs:
                column_edges.append(
                    ColumnEdge(
                        source_table=r.table,
                        source_column=r.column,
                        target_table=stmt.target,
                        target_column=col.name,
                        script=result.filename,
                        line=r.line,
                        expr=r.expr,
                        transforms=list(r.transforms),
                        star_expanded=r.star,
                        union_branch=r.branch,
                        target_kind=kind,
                    )
                )

    diagnostics = [d for d in errors.values()]
    return {
        "scripts": [asdict(s) for s in results],
        "table_edges": [asdict(e) for e in table_edges],
        "column_edges": [asdict(e) for e in column_edges],
        "target_kinds": target_kind,
        "catalog": {k: v for k, v in catalog.items()},
        "diagnostics": [asdict(d) for d in diagnostics],
        "parsed_count": sum(1 for s in results if s.status == "ok"),
        "failed_count": sum(1 for s in results if s.status == "error"),
    }
