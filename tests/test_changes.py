"""整组变更演练：候选 schema 预演、星号透传、跨层改名、类型传播、失败回滚与幂等指纹。

重点覆盖用户指出的两个旧清单缺陷：
1. SELECT * / t.* 星号透传的新列 / 改名列不能漏；
2. 改名要沿列级血缘跨层传播，不能只看源表显式引用。
"""
from __future__ import annotations

import pytest

from app import changes, pipeline


@pytest.fixture(scope="module")
def env(sample_dir):
    base_schemas = pipeline.load_schemas(sample_dir)
    files = pipeline.load_sample_files(sample_dir)
    analysis = pipeline.analyze(files, base_schemas)
    return base_schemas, files, analysis


def _three_changes():
    return changes.changes_from_dicts(
        [
            {"table": "ods_products", "action": "add_column", "new_name": "brand_code", "new_type": "varchar(32)"},
            {"table": "ods_products", "action": "rename_column", "column": "sku", "new_name": "product_sku"},
            {"table": "ods_orders", "action": "alter_type", "column": "amount", "new_type": "decimal(18,4)"},
        ]
    )


def test_fingerprint_is_stable_and_order_independent(env):
    base_schemas, files, analysis = env
    a = _three_changes()
    b = list(reversed(a))
    fp1 = changes.changes_fingerprint(a, "run_x")
    fp2 = changes.changes_fingerprint(b, "run_x")
    assert fp1 == fp2  # 同组变更顺序不同也算同一组
    fp3 = changes.changes_fingerprint(a, "run_y")
    assert fp1 != fp3  # 基线不同，指纹不同


def test_candidate_schema_applies_all_changes(env):
    base_schemas, _, _ = env
    candidate = changes.candidate_schemas(base_schemas, _three_changes())
    products = candidate["ods_products"]
    assert "sku" not in products
    assert "product_sku" in products
    assert "brand_code" in products
    # 入参不被修改
    assert "sku" in base_schemas["ods_products"]
    # alter_type 不改列名、不增删列
    assert candidate["ods_orders"] == base_schemas["ods_orders"]


def test_rehearse_marks_star_passthrough_for_new_and_renamed_columns(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)

    vps = next(o for o in plan["affected_objects"] if o["object"] == "v_product_sales")
    # v_product_sales 是 SELECT * 透传，命中方式必须是 star，且要重建视图
    assert vps["kind"] == "view" and vps["rebuild"] is True
    assert set(vps["star_columns"]) == {"sku", "brand_code"}
    assert vps["explicit_columns"] == []

    # 新增列 brand_code 经星号自动透传（基线没有该列边，旧清单会漏）
    add_chain = plan["add_propagation"][0]["downstream_chain"]
    assert len(add_chain) == 1
    assert add_chain[0]["object"] == "v_product_sales"
    assert add_chain[0]["hits"] == ["star"]
    assert add_chain[0]["newly_added"] is True
    assert add_chain[0]["candidate_columns"] == ["brand_code"]


def test_rename_propagates_across_layer_with_old_and_new_names(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    rename = plan["rename_propagation"][0]
    assert (rename["table"], rename["from_column"], rename["to_column"]) == (
        "ods_products",
        "sku",
        "product_sku",
    )
    chain = rename["downstream_chain"]
    assert len(chain) == 1
    node = chain[0]
    assert node["object"] == "v_product_sales"
    assert node["base_columns"] == ["sku"]
    assert node["candidate_columns"] == ["product_sku"]
    assert node["renamed"] is True
    assert node["hits"] == ["star"]


def test_alter_type_propagates_through_explicit_and_star_and_cast(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    tp = plan["type_propagation"][0]
    assert (tp["table"], tp["column"], tp["new_type"]) == ("ods_orders", "amount", "decimal(18,4)")
    objs = {d["object"]: d for d in tp["downstream_chain"]}
    # 5 个承接 amount 身份的下游对象
    assert set(objs) == {
        "dwd_order",
        "v_channel_sales",
        "dws_customer_order",
        "dws_order_wide",
        "ads_customer_rank",
    }
    # dwd_order 是显式 CAST；dws_order_wide 是 o.* 星号透传
    assert "explicit" in objs["dwd_order"]["hits"]
    assert ["CAST"] in objs["dwd_order"]["transforms"]
    assert "star" in objs["dws_order_wide"]["hits"]


def test_rebuild_views_and_backfill_tables_are_classified(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    assert set(plan["rebuild_views"]) == {"v_product_sales", "v_channel_sales"}
    assert set(plan["backfill_tables"]) == {
        "dwd_order",
        "dws_customer_order",
        "dws_order_wide",
        "ads_customer_rank",
    }
    # 每个对象都带层级，且按层级从浅到深
    levels = [o["level"] for o in plan["affected_objects"]]
    assert levels == sorted(levels)


def test_replay_is_green_for_compatible_change_group(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    assert plan["ready"] is True
    assert plan["replay"]["ok"] is True
    assert plan["replay"]["failed_count"] == 0
    assert plan["replay"]["parsed_count"] == 12
    # 候选 catalog 里视图已带出新列与改名列
    assert "brand_code" in plan["candidate_catalog"]["v_product_sales"]
    assert "product_sku" in plan["candidate_catalog"]["v_product_sales"]
    assert "sku" not in plan["candidate_catalog"]["v_product_sales"]


def test_ddl_preview_covers_all_three_changes(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    ddl = " ".join(plan["ddl_preview"])
    assert "ADD COLUMN brand_code varchar(32)" in ddl
    assert "RENAME COLUMN sku TO product_sku" in ddl
    assert "ALTER COLUMN amount TYPE decimal(18,4)" in ddl


def test_validation_rejects_unknown_table_duplicate_and_missing_names(env):
    base_schemas, files, analysis = env
    with pytest.raises(changes.ChangeValidationError):
        changes.rehearse(
            changes.changes_from_dicts([{"table": "nope", "action": "add_column", "new_name": "x"}]),
            base_schemas, files, "run_base", analysis,
        )
    with pytest.raises(changes.ChangeValidationError):
        # 新增已存在的列
        changes.rehearse(
            changes.changes_from_dicts([{"table": "ods_orders", "action": "add_column", "new_name": "amount"}]),
            base_schemas, files, "run_base", analysis,
        )
    with pytest.raises(changes.ChangeValidationError):
        # 改名缺少新列名
        changes.rehearse(
            changes.changes_from_dicts([{"table": "ods_orders", "action": "rename_column", "column": "amount"}]),
            base_schemas, files, "run_base", analysis,
        )
    with pytest.raises(changes.ChangeValidationError):
        changes.rehearse([], base_schemas, files, "run_base", analysis)


def test_failed_replay_blocks_apply_and_keeps_base_structure(env):
    base_schemas, files, analysis = env
    # 自定义脚本显式引用 amount；把 amount 改名后候选重放必失败
    custom_files = files + [
        {
            "filename": "99_explicit_amount.sql",
            "sql": "CREATE TABLE t_explicit AS SELECT o.amount AS amount FROM ods_orders o;",
        }
    ]
    custom_analysis = pipeline.analyze(custom_files, base_schemas)
    chg = changes.changes_from_dicts(
        [{"table": "ods_orders", "action": "rename_column", "column": "amount", "new_name": "order_amount"}]
    )
    plan = changes.rehearse(chg, base_schemas, custom_files, "run_custom", custom_analysis)
    assert plan["ready"] is False
    assert plan["replay"]["failed_count"] >= 1
    assert any("amount" in f["message"] for f in plan["replay"]["failed_scripts"])

    # 即使人工 approve，重放未绿也必须 rejected，且基线结构不动
    decided = changes.apply_decision(plan, True)
    assert decided["status"] == changes.REJECTED
    assert "amount" in decided["base_schemas"]["ods_orders"]
    assert decided["ready"] is False


def test_apply_decision_terminal_state_is_sticky(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    applied = changes.apply_decision(plan, True)
    assert applied["status"] == changes.APPLIED
    # 终态后再决策不改变结果
    assert changes.apply_decision(applied, False)["status"] == changes.APPLIED

    plan2 = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    rejected = changes.apply_decision(plan2, False)
    assert rejected["status"] == changes.REJECTED
    assert rejected["rollback"]


def test_reject_keeps_base_structure(env):
    base_schemas, files, analysis = env
    plan = changes.rehearse(_three_changes(), base_schemas, files, "run_base", analysis)
    rejected = changes.apply_decision(plan, False)
    assert rejected["status"] == changes.REJECTED
    # 原结构保留：sku 仍在、brand_code 不在、amount 列仍在
    assert "sku" in rejected["base_schemas"]["ods_products"]
    assert "brand_code" not in rejected["base_schemas"]["ods_products"]
    assert "amount" in rejected["base_schemas"]["ods_orders"]
