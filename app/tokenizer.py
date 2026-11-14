"""轻量 SQL 词法扫描（只用标准库）。

把一条脚本切成带「行 / 列 / 字符偏移」的 token，供 parser.py 做结构解析。
不追求完整 SQL 方言，只覆盖血缘分析需要的子集；无法识别的内容一律抛
LexError 并给出准确位置，绝不静默跳过。
"""
from __future__ import annotations

from dataclasses import dataclass

# 只收录会改变结构的保留字；函数名（CAST/ROUND/COALESCE...）按 ident 处理，
# 由 parser 依据「后面紧跟左括号」判定为函数调用。
KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "LIMIT",
    "INSERT", "INTO", "CREATE", "TABLE", "VIEW", "AS", "JOIN", "LEFT",
    "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "ON", "UNION", "ALL",
    "DISTINCT", "REPLACE", "OR", "AND", "NOT", "EXCEPT", "INTERSECT",
    "WITH", "USING", "ASC", "DESC",
}

PUNCT = set("(),.;*=<>+-/%[]:|!")


class LexError(Exception):
    """词法错误：带行列位置，供诊断模块直接展示。"""

    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col


@dataclass(frozen=True)
class Token:
    kind: str  # ident | keyword | number | string | punct | eof
    text: str
    upper: str
    line: int
    col: int
    offset: int

    def is_punct(self, ch: str) -> bool:
        return self.kind == "punct" and self.text == ch

    def is_kw(self, *names: str) -> bool:
        return self.kind == "keyword" and self.upper in names


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(sql)
    line = 1
    col = 1

    def advance(count: int = 1) -> None:
        nonlocal i, line, col
        for _ in range(count):
            if i >= n:
                return
            if sql[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        ch = sql[i]
        start_line, start_col, start_off = line, col, i

        if ch in " \t\r\n":
            advance()
            continue

        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                advance()
            continue

        # 块注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            close = sql.find("*/", i + 2)
            if close < 0:
                raise LexError("块注释未闭合，缺少 */", start_line, start_col)
            advance(close + 2 - i)
            continue

        # 字符串字面量
        if ch == "'":
            advance()
            buf: list[str] = []
            closed = False
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        advance(2)
                        continue
                    advance()
                    closed = True
                    break
                buf.append(sql[i])
                advance()
            if not closed:
                raise LexError("字符串常量未闭合，缺少结束单引号", start_line, start_col)
            tokens.append(Token("string", "".join(buf), "".join(buf), start_line, start_col, start_off))
            continue

        # 带引号的标识符
        if ch == '"':
            advance()
            buf = []
            closed = False
            while i < n:
                if sql[i] == '"':
                    advance()
                    closed = True
                    break
                buf.append(sql[i])
                advance()
            if not closed:
                raise LexError('带引号的标识符未闭合，缺少结束双引号', start_line, start_col)
            name = "".join(buf)
            tokens.append(Token("ident", name, name.upper(), start_line, start_col, start_off))
            continue

        # 数字
        if ch.isdigit():
            buf = [ch]
            advance()
            while i < n and (sql[i].isdigit() or sql[i] == "."):
                buf.append(sql[i])
                advance()
            text = "".join(buf)
            tokens.append(Token("number", text, text, start_line, start_col, start_off))
            continue

        # 标识符 / 关键字
        if ch.isalpha() or ch == "_":
            buf = [ch]
            advance()
            while i < n and (sql[i].isalnum() or sql[i] in "_$"):
                buf.append(sql[i])
                advance()
            text = "".join(buf)
            upper = text.upper()
            kind = "keyword" if upper in KEYWORDS else "ident"
            tokens.append(Token(kind, text, upper, start_line, start_col, start_off))
            continue

        if ch in PUNCT:
            tokens.append(Token("punct", ch, ch, start_line, start_col, start_off))
            advance()
            continue

        raise LexError(f"无法识别的字符 {ch!r}", start_line, start_col)

    tokens.append(Token("eof", "", "", line, col, n))
    return tokens
