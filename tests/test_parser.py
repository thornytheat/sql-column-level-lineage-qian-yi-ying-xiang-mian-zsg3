"""轻量解析器：别名 / 子查询 / JOIN / UNION / * 展开 / 错误定位。"""
from __future__ import annotations

import pathlib

import pytest

from app import parser


def _read(sample_dir: pathlib.Path, name: str) -> str:
    return (sample_dir / "sql" / name).read_text(encoding="utf-8")


def test_alias_and_function_chain(sample_dir):
    stmt = parser.parse_script(_read(sample_dir, "01_dwd_customer.sql"), "01.sql")
    assert stmt.kind == "ctas"
    assert stmt.target == "dwd_customer"
    names = [item.alias for item in stmt.select.items]
    assert names == [None, "customer_name", "city", None]
    city = stmt.select.items[2]
    assert city.transforms == ["UPPER", "TRIM"]
    assert [(r.qualifier, r.column) for r in city.refs] == [("c", "city")]


def test_insert_select_with_target_column_list(sample_dir):
    stmt = parser.parse_script(_read(sample_dir, "03_dwd_order.sql"), "03.sql")
    assert stmt.kind == "insert_select"
    assert stmt.target_columns == ["order_id", "customer_id", "amount", "status", "channel", "created_at"]
    assert parser.output_names(stmt.select) == stmt.target_columns
    amount_item = stmt.select.items[2]
    assert amount_item.transforms == ["CAST"]
    assert amount_item.refs[0].column == "amount"


def test_subquery_in_from(sample_dir):
    stmt = parser.parse_script(_read(sample_dir, "04_dwd_order_item.sql"), "04.sql")
    assert len(stmt.select.froms) == 1
    ref = stmt.select.froms[0]
    assert ref.table is None and ref.alias == "t"
    assert ref.subquery is not None
    assert [i.alias for i in ref.subquery.items] == [None, None, None, None, None]
    assert [r.column for r in ref.subquery.items[3].refs] == ["qty"]


def test_join_and_on_is_not_a_column_ref(sample_dir):
    stmt = parser.parse_script(_read(sample_dir, "05_dws_customer_order.sql"), "05.sql")
    aliases = [r.alias for r in stmt.select.froms]
    assert aliases == ["c", "o"]
    # ON 条件不参与血缘，不应出现在选择列表的 refs 里
    refs = [r.column for item in stmt.select.items for r in item.refs]
    assert refs == ["customer_id", "customer_name", "order_id", "amount", "status"]


def test_union_all_keeps_both_branches(sample_dir):
    stmt = parser.parse_script(_read(sample_dir, "06_view_channel_sales.sql"), "06.sql")
    assert len(stmt.select.unions) == 1
    branch = stmt.select.unions[0]
    assert [item.alias for item in branch.items] == ["channel", "amount"]
    assert stmt.select.items[1].transforms == ["CAST"]
    assert branch.items[1].transforms == ["COALESCE"]


def test_star_and_qualified_star(sample_dir):
    unqualified = parser.parse_script(_read(sample_dir, "07_view_product_sales.sql"), "07.sql")
    assert unqualified.select.items[0].star is True
    assert unqualified.select.items[0].star_qualifier is None
    assert parser.output_names(unqualified.select) == [None]

    qualified = parser.parse_script(_read(sample_dir, "08_dws_order_wide.sql"), "08.sql")
    assert qualified.select.items[0].star is True
    assert qualified.select.items[0].star_qualifier == "o"
    assert parser.output_names(qualified.select)[1:] == ["customer_name", "city"]


def test_syntax_error_position(sample_dir):
    broken = (sample_dir / "sql_broken" / "13_broken_channel.sql").read_text(encoding="utf-8")
    with pytest.raises(parser.ParseError) as excinfo:
        parser.parse_script(broken, "13_broken_channel.sql")
    err = excinfo.value
    assert "括号未闭合" in err.message
    assert (err.line, err.col) == (5, 8)


def test_unsupported_statement_type():
    with pytest.raises(parser.ParseError) as excinfo:
        parser.parse_script("UPDATE dwd_order SET amount = 1;", "update.sql")
    assert "无法识别的语句类型" in excinfo.value.message


def test_multi_statement_rejected(sample_dir):
    sql = _read(sample_dir, "01_dwd_customer.sql") + "\n" + _read(sample_dir, "02_view_customer.sql")
    with pytest.raises(parser.ParseError) as excinfo:
        parser.parse_script(sql, "double.sql")
    assert "只支持一条语句" in excinfo.value.message


def test_with_clause_reports_unsupported():
    with pytest.raises(parser.ParseError) as excinfo:
        parser.parse_script("WITH t AS (SELECT 1) SELECT * FROM t;", "cte.sql")
    assert "WITH" in excinfo.value.message


def test_unknown_from_table_is_reported_not_skipped():
    with pytest.raises(parser.ParseError) as excinfo:
        parser.parse_script("CREATE VIEW v AS SELECT a FROM;", "bad_from.sql")
    assert excinfo.value.line >= 1
