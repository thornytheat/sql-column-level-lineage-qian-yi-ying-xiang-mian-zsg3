"""轻量 SQL 结构解析（标准库实现，不依赖 sqlparse）。

只解析血缘分析需要的子集：
- CREATE TABLE|VIEW ... AS SELECT / INSERT INTO ... SELECT
- 选择列表（含 AS 别名、隐式别名、函数包裹、t.* 与 *）
- FROM 单表 / 多表 / JOIN ... ON / 嵌套子查询
- UNION ALL（多分支按列序号对齐）
- WHERE / GROUP BY / ORDER BY / LIMIT 仅做结构跳过，不参与血缘

语法不支持或写错时抛 ParseError（带行、列与中文原因），pipeline 会把它
记成「解析失败」的诊断，绝不静默跳过。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .tokenizer import LexError, Token, tokenize


class ParseError(Exception):
    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col


@dataclass
class ColumnRef:
    qualifier: str | None
    column: str
    line: int
    col: int


@dataclass
class SelectItem:
    expr: str
    alias: str | None
    line: int
    col: int
    star: bool = False
    star_qualifier: str | None = None
    refs: list[ColumnRef] = field(default_factory=list)
    transforms: list[str] = field(default_factory=list)


@dataclass
class TableRef:
    table: str | None
    alias: str | None
    subquery: "Select | None"
    line: int
    col: int


@dataclass
class Select:
    items: list[SelectItem]
    froms: list[TableRef]
    unions: list["Select"]
    line: int

    def branches(self) -> list["Select"]:
        return [self] + list(self.unions)


@dataclass
class Statement:
    filename: str
    kind: str  # ctas | insert_select | view
    target: str
    target_columns: list[str] | None
    select: Select
    line: int


# --------------------------------------------------------------------------
# 扫描工具
# --------------------------------------------------------------------------

def _find(
    tokens: list[Token],
    start: int,
    stop_uppers: tuple[str, ...] = (),
    stop_puncts: tuple[str, ...] = (),
) -> int:
    """返回 start 之后第一个「括号深度为 0」的停止 token 下标，找不到返回 -1。

    扫描过程中检测括号失配，并抛出带位置的中文错误。
    """
    stack: list[Token] = []
    for i in range(start, len(tokens)):
        t = tokens[i]
        if t.kind == "eof":
            break
        if t.is_punct("("):
            stack.append(t)
            continue
        if t.is_punct(")"):
            if not stack:
                raise ParseError("多余的右括号 )，没有与之匹配的 (", t.line, t.col)
            stack.pop()
            continue
        if stack:
            continue
        if t.kind == "keyword" and t.upper in stop_uppers:
            return i
        if t.kind == "punct" and t.text in stop_puncts:
            return i
    if stack:
        bad = stack[-1]
        raise ParseError("括号未闭合，缺少与之匹配的 )", bad.line, bad.col)
    return -1


def _split_top(tokens: list[Token], sep: str) -> list[list[Token]]:
    parts: list[list[Token]] = []
    cur: list[Token] = []
    depth = 0
    for t in tokens:
        if t.is_punct("("):
            depth += 1
        elif t.is_punct(")"):
            depth -= 1
        if depth == 0 and t.is_punct(sep):
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def _match_paren(tokens: list[Token], start: int) -> int:
    depth = 0
    for i in range(start, len(tokens)):
        t = tokens[i]
        if t.is_punct("("):
            depth += 1
        elif t.is_punct(")"):
            depth -= 1
            if depth == 0:
                return i
    open_tok = tokens[start]
    raise ParseError("子查询括号未闭合，缺少与之匹配的 )", open_tok.line, open_tok.col)


def _relation_name(tokens: list[Token], i: int) -> tuple[str, int]:
    t = tokens[i]
    if t.kind != "ident":
        raise ParseError(f"期望表名，实际是 {t.text or '语句结尾'!r}", t.line, t.col)
    name = t.text
    i += 1
    while i + 1 < len(tokens) and tokens[i].is_punct(".") and tokens[i + 1].kind in {"ident", "keyword"}:
        name += "." + tokens[i + 1].text
        i += 2
    return name, i


def _ident_list(tokens: list[Token], i: int) -> tuple[list[str], int]:
    # tokens[i] 是 '('
    cols: list[str] = []
    i += 1
    while i < len(tokens):
        t = tokens[i]
        if t.is_punct(")"):
            if not cols:
                raise ParseError("列清单为空", t.line, t.col)
            return cols, i + 1
        if t.kind != "ident":
            raise ParseError(f"列清单中期望列名，实际是 {t.text!r}", t.line, t.col)
        cols.append(t.text)
        i += 1
        if i < len(tokens) and tokens[i].is_punct(","):
            i += 1
            continue
    raise ParseError("列清单括号未闭合，缺少 )", tokens[-1].line, tokens[-1].col)


# --------------------------------------------------------------------------
# 选择列表
# --------------------------------------------------------------------------

def _parse_item(sql: str, part: list[Token]) -> SelectItem:
    first = part[0]
    if len(part) == 1 and part[0].is_punct("*"):
        return SelectItem(expr="*", alias=None, star=True, line=first.line, col=first.col)
    if len(part) == 3 and part[0].kind == "ident" and part[1].is_punct(".") and part[2].is_punct("*"):
        return SelectItem(
            expr=f"{part[0].text}.*",
            alias=None,
            star=True,
            star_qualifier=part[0].text,
            line=first.line,
            col=first.col,
        )

    expr_tokens = part
    alias: str | None = None
    if len(part) >= 3 and part[-2].is_kw("AS") and part[-1].kind == "ident":
        alias = part[-1].text
        expr_tokens = part[:-2]
    elif (
        len(part) >= 2
        and part[-1].kind == "ident"
        and not part[-2].is_punct(".")
        and not part[-2].is_punct("(")
        and not part[-2].is_kw("AS")
    ):
        alias = part[-1].text
        expr_tokens = part[:-1]
    if not expr_tokens:
        raise ParseError("选择列表中存在空表达式", first.line, first.col)

    last = expr_tokens[-1]
    expr = sql[expr_tokens[0].offset: last.offset + len(last.text)].strip()

    refs: list[ColumnRef] = []
    transforms: list[str] = []
    k = 0
    while k < len(expr_tokens):
        t = expr_tokens[k]
        if t.kind == "ident":
            nxt = expr_tokens[k + 1] if k + 1 < len(expr_tokens) else None
            prev = expr_tokens[k - 1] if k > 0 else None
            if nxt is not None and nxt.is_punct("("):
                if t.upper not in TYPE_NAMES:
                    transforms.append(t.text)
                k += 1
                continue
            if prev is not None and prev.is_punct("."):
                k += 1
                continue
            if nxt is not None and nxt.is_punct("."):
                col_tok = expr_tokens[k + 2] if k + 2 < len(expr_tokens) else None
                if col_tok is not None and col_tok.kind == "ident":
                    refs.append(
                        ColumnRef(qualifier=t.text, column=col_tok.text, line=col_tok.line, col=col_tok.col)
                    )
                    k += 3
                    continue
                k += 1
                continue
            refs.append(ColumnRef(None, t.text, t.line, t.col))
        k += 1

    return SelectItem(
        expr=expr,
        alias=alias,
        line=first.line,
        col=first.col,
        refs=refs,
        transforms=transforms,
    )


def _parse_items(sql: str, tokens: list[Token]) -> list[SelectItem]:
    if not tokens:
        raise ParseError("SELECT 后缺少选择列表", 1, 1)
    items = [_parse_item(sql, part) for part in _split_top(tokens, ",")]
    if not items:
        raise ParseError("SELECT 后缺少选择列表", tokens[0].line, tokens[0].col)
    return items


# --------------------------------------------------------------------------
# FROM 子句
# --------------------------------------------------------------------------

def _parse_from(sql: str, tokens: list[Token]) -> list[TableRef]:
    refs: list[TableRef] = []
    i = 0
    n = len(tokens)
    skip_kw = ("LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "JOIN")
    while i < n:
        t = tokens[i]
        if t.kind == "eof":
            break
        if t.is_punct(",") or (t.kind == "keyword" and t.upper in skip_kw):
            i += 1
            continue
        if t.is_kw("ON", "USING"):
            i += 1
            depth = 0
            while i < n:
                tt = tokens[i]
                if tt.kind == "eof":
                    break
                if tt.is_punct("("):
                    depth += 1
                elif tt.is_punct(")"):
                    depth -= 1
                if depth == 0 and (
                    tt.is_punct(",") or (tt.kind == "keyword" and tt.upper in skip_kw)
                ):
                    break
                i += 1
            continue
        if t.is_punct("("):
            close = _match_paren(tokens, i)
            inner = tokens[i + 1: close]
            sub, _ = _parse_select(sql, inner, 0)
            i = close + 1
            alias = None
            if i < n and tokens[i].is_kw("AS"):
                i += 1
            if i < n and tokens[i].kind == "ident":
                alias = tokens[i].text
                i += 1
            refs.append(TableRef(table=None, alias=alias, subquery=sub, line=t.line, col=t.col))
            continue
        if t.kind == "ident":
            name = t.text
            i += 1
            while i + 1 < n and tokens[i].is_punct(".") and tokens[i + 1].kind in {"ident", "keyword"}:
                name += "." + tokens[i + 1].text
                i += 2
            alias = None
            if i < n and tokens[i].is_kw("AS"):
                i += 1
            if i < n and tokens[i].kind == "ident":
                alias = tokens[i].text
                i += 1
            refs.append(TableRef(table=name, alias=alias, subquery=None, line=t.line, col=t.col))
            continue
        raise ParseError(f"FROM 子句中出现无法解析的内容 {t.text!r}", t.line, t.col)
    if not refs:
        tok = tokens[0] if tokens else Token("eof", "", "", 1, 1, 0)
        raise ParseError("FROM 子句为空，至少需要一个来源表或子查询", tok.line, tok.col)
    return refs


# --------------------------------------------------------------------------
# SELECT 与语句
# --------------------------------------------------------------------------

# CAST(x AS DECIMAL(18,2)) 里的类型名后跟括号，但它不是函数调用
TYPE_NAMES = {
    "DECIMAL", "NUMERIC", "VARCHAR", "CHAR", "INT", "INTEGER", "BIGINT", "SMALLINT",
    "TIMESTAMP", "DATE", "DATETIME", "DOUBLE", "FLOAT", "BOOLEAN", "TEXT", "STRING",
}

_CLAUSES = ("WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "EXCEPT", "INTERSECT")


def _parse_select(sql: str, tokens: list[Token], start: int) -> tuple[Select, int]:
    sel_tok = tokens[start]
    if not sel_tok.is_kw("SELECT"):
        raise ParseError(f"期望 SELECT，实际是 {sel_tok.text!r}", sel_tok.line, sel_tok.col)
    i = start + 1
    if i < len(tokens) and tokens[i].is_kw("DISTINCT"):
        i += 1

    from_idx = _find(tokens, i, ("FROM",))
    if from_idx < 0:
        raise ParseError("未找到 FROM 子句，无法建立列级血缘", sel_tok.line, sel_tok.col)

    items = _parse_items(sql, tokens[i:from_idx])

    j = from_idx + 1
    end = _find(tokens, j, _CLAUSES, (";",))
    if end < 0:
        from_tokens = tokens[j:]
        if from_tokens and from_tokens[-1].kind == "eof":
            from_tokens = from_tokens[:-1]
            next_index = len(tokens) - 1  # 顶层结束在 eof
        else:
            next_index = len(tokens)  # 子查询切片：结尾即切片的右边界
        tail = None
    else:
        tail = tokens[end]
        from_tokens = tokens[j:end]
        next_index = end

    froms = _parse_from(sql, from_tokens)
    unions: list[Select] = []

    if tail is not None and tail.kind == "keyword" and tail.upper in ("UNION", "EXCEPT", "INTERSECT"):
        k = end + 1
        if k < len(tokens) and tokens[k].is_kw("ALL", "DISTINCT"):
            k += 1
        branch, next_index = _parse_select(sql, tokens, k)
        unions = [branch]
    elif tail is not None and tail.kind == "keyword":
        # WHERE / GROUP BY / ORDER BY / HAVING / LIMIT：仅跳过，不参与血缘
        semi = _find(tokens, end, (), (";",))
        next_index = semi if semi >= 0 else len(tokens) - 1

    return Select(items=items, froms=froms, unions=unions, line=sel_tok.line), next_index


def _parse_statement(sql: str, tokens: list[Token], filename: str) -> tuple[Statement, int]:
    t = tokens[0]
    if t.is_kw("WITH"):
        raise ParseError("暂不支持 WITH 子句，请改写成子查询形式", t.line, t.col)
    if t.is_kw("CREATE"):
        i = 1
        if i < len(tokens) and tokens[i].is_kw("OR"):
            i += 1
            if not (i < len(tokens) and tokens[i].is_kw("REPLACE")):
                tok = tokens[i] if i < len(tokens) else t
                raise ParseError("CREATE OR 后缺少 REPLACE", tok.line, tok.col)
            i += 1
        if i >= len(tokens):
            raise ParseError("CREATE 后缺少 TABLE 或 VIEW", t.line, t.col)
        kind_tok = tokens[i]
        if kind_tok.is_kw("TABLE"):
            kind = "ctas"
        elif kind_tok.is_kw("VIEW"):
            kind = "view"
        else:
            raise ParseError(
                f"仅支持 CREATE TABLE/VIEW ... AS SELECT，实际是 CREATE {kind_tok.text}",
                kind_tok.line,
                kind_tok.col,
            )
        i += 1
        target, i = _relation_name(tokens, i)
        target_columns = None
        if i < len(tokens) and tokens[i].is_punct("("):
            target_columns, i = _ident_list(tokens, i)
        if not (i < len(tokens) and tokens[i].is_kw("AS")):
            tok = tokens[i] if i < len(tokens) else t
            raise ParseError(
                f"CREATE {kind_tok.text} 缺少 AS SELECT，无法确定输出列", tok.line, tok.col
            )
        i += 1
        if i < len(tokens) and tokens[i].is_kw("WITH"):
            raise ParseError("暂不支持 AS WITH 形式，请改写成子查询", tokens[i].line, tokens[i].col)
        select, next_index = _parse_select(sql, tokens, i)
        return Statement(
            filename=filename,
            kind=kind,
            target=target,
            target_columns=target_columns,
            select=select,
            line=t.line,
        ), next_index

    if t.is_kw("INSERT"):
        i = 1
        if not (i < len(tokens) and tokens[i].is_kw("INTO")):
            tok = tokens[i] if i < len(tokens) else t
            raise ParseError("仅支持 INSERT INTO ... SELECT，缺少 INTO", tok.line, tok.col)
        i += 1
        target, i = _relation_name(tokens, i)
        target_columns = None
        if i < len(tokens) and tokens[i].is_punct("("):
            target_columns, i = _ident_list(tokens, i)
        if not (i < len(tokens) and tokens[i].is_kw("SELECT")):
            tok = tokens[i] if i < len(tokens) else t
            raise ParseError("INSERT INTO 的目标表后必须是 SELECT 子查询", tok.line, tok.col)
        select, next_index = _parse_select(sql, tokens, i)
        return Statement(
            filename=filename,
            kind="insert_select",
            target=target,
            target_columns=target_columns,
            select=select,
            line=t.line,
        ), next_index

    raise ParseError(
        f"无法识别的语句类型 {t.text!r}；只支持 CREATE TABLE/VIEW ... AS SELECT 与 INSERT INTO ... SELECT",
        t.line,
        t.col,
    )


def parse_script(sql: str, filename: str = "<inline>") -> Statement:
    try:
        tokens = tokenize(sql)
    except LexError as exc:
        raise ParseError(exc.message, exc.line, exc.col) from exc
    if not tokens or tokens[0].kind == "eof":
        raise ParseError("脚本为空，没有可解析的语句", 1, 1)
    stmt, next_index = _parse_statement(sql, tokens, filename)
    idx = next_index
    if idx < len(tokens) and tokens[idx].is_punct(";"):
        idx += 1
    if idx < len(tokens) and tokens[idx].kind != "eof":
        tok = tokens[idx]
        raise ParseError(
            f"一个脚本只支持一条语句，此处存在多余内容 {tok.text!r}", tok.line, tok.col
        )
    return stmt


def output_names(select: Select) -> list[str | None]:
    """静态推导输出列名（不依赖 schema）。

    - AS 别名 / 隐式别名优先；
    - 单列引用取列名；
    - 单层函数包裹取 函数名_源列名（如 SUM(amount) -> sum_amount）；
    - `*` / `t.*` 返回 None（等 schema 展开后确定）。
    """
    names: list[str | None] = []
    for item in select.items:
        if item.star:
            names.append(None)
            continue
        if item.alias:
            names.append(item.alias)
            continue
        if len(item.refs) == 1 and not item.transforms:
            names.append(item.refs[0].column)
            continue
        if len(item.refs) == 1 and item.transforms:
            names.append(f"{item.transforms[0].lower()}_{item.refs[0].column}")
            continue
        names.append(None)
    return names
