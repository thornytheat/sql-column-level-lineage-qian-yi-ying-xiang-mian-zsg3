"""API 与端到端：成功链路、4xx 诊断定位、影响面/迁移清单、导出、页面。"""
from __future__ import annotations

BROKEN_SQL = """CREATE VIEW v_custom_bad AS
SELECT
  o.order_id AS order_id,
  ROUND(o.amount, 2 AS amount
FROM ods_orders o;
"""

GOOD_SQL = """CREATE TABLE ads_order_amount AS
SELECT
  o.order_id AS order_id,
  CAST(o.amount AS DECIMAL(18,2)) AS amount
FROM ods_orders o;
"""


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["sample_scripts"] == 12
    assert body["broken_scripts"] == 1
    assert body["schema_tables"] == 7
    assert body["runs"] >= 1  # 启动时已 seed 一次基线解析


def test_create_sample_run(client):
    res = client.post("/api/runs", json={"mode": "sample"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["parsed_count"] == 12
    assert body["failed_count"] == 0
    assert body["column_edge_count"] == 53
    assert body["table_edge_count"] == 17
    assert body["cycle_count"] == 1
    assert body["diagnostics"] == []


def test_create_run_with_broken_script_keeps_others(client):
    res = client.post("/api/runs", json={"mode": "sample_broken"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["script_count"] == 13
    assert body["parsed_count"] == 12
    assert body["failed_count"] == 1
    err = body["diagnostics"][0]
    assert err["filename"] == "13_broken_channel.sql"
    assert (err["line"], err["col"]) == (5, 8)
    assert "括号未闭合" in err["message"]


def test_broken_sql_returns_422_with_position(client):
    res = client.post("/api/runs", json={"mode": "custom", "sql": BROKEN_SQL, "filename": "bad.sql"})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "未能解析" in detail["message"]
    first = detail["errors"][0]
    assert first["filename"] == "bad.sql"
    assert first["line"] == 4 and first["col"] == 8
    assert "括号未闭合" in first["message"]


def test_custom_sql_creates_lineage(client):
    res = client.post("/api/runs", json={"mode": "custom", "sql": GOOD_SQL, "filename": "ads_order_amount.sql"})
    assert res.status_code == 200, res.text
    run_id = res.json()["run_id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["parsed_count"] == 1
    group = next(g for g in detail["column_groups"] if g["target_table"] == "ads_order_amount")
    amount = next(c for c in group["columns"] if c["target_column"] == "amount")
    assert amount["sources"][0]["source"] == "ods_orders.amount"
    assert amount["sources"][0]["transforms"] == ["CAST"]
    assert amount["sources"][0]["evidence"] == "ads_order_amount.sql:4"


def test_get_run_detail(client, sample_run_id):
    res = client.get(f"/api/runs/{sample_run_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["run"]["cycle_count"] == 1
    assert body["levels"]["dwd_order"] == 1
    targets = {g["target_table"] for g in body["column_groups"]}
    assert {"dwd_customer", "dwd_order", "v_channel_sales", "dws_customer_order"} <= targets


def test_impact_api(client, sample_run_id):
    res = client.post(
        "/api/impact", json={"run_id": sample_run_id, "table": "ods_orders", "column": "amount"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["object_count"] == 5
    assert body["max_level"] == 3
    assert body["direct_count"] == 2
    objects = [it["object"] for it in body["downstream"]]
    assert objects[0] == "dwd_order"
    assert body["downstream"][-1]["level"] == 3


def test_impact_unknown_table_is_400(client, sample_run_id):
    res = client.post(
        "/api/impact", json={"run_id": sample_run_id, "table": "not_exist_table", "column": "x"}
    )
    assert res.status_code == 400
    assert "没有任何下游边" in res.json()["detail"]


def test_migration_api(client, sample_run_id):
    res = client.post(
        "/api/migration",
        json={"run_id": sample_run_id, "table": "ods_orders", "column": "amount", "operation": "drop"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["step_count"] == 6
    assert body["ddl_sql"] == "ALTER TABLE ods_orders DROP COLUMN amount;"
    assert [s["action"] for s in body["steps"]] == [
        "freeze",
        "rewrite",
        "rewrite",
        "rewrite",
        "backfill",
        "ddl",
    ]


def test_migration_rename_requires_new_name(client, sample_run_id):
    res = client.post(
        "/api/migration",
        json={"run_id": sample_run_id, "table": "ods_orders", "column": "amount", "operation": "rename"},
    )
    assert res.status_code == 400
    assert "new_name" in res.json()["detail"]


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/run_nope").status_code == 404
    assert client.post("/api/impact", json={"run_id": "run_nope", "table": "ods_orders"}).status_code == 404


def test_export_json_and_csv(client, sample_run_id):
    js = client.get(f"/api/runs/{sample_run_id}/export?format=json")
    assert js.status_code == 200
    payload = js.json()
    assert payload["run"]["run_id"] == sample_run_id
    assert len(payload["column_edges"]) == 53
    assert "attachment" in js.headers["content-disposition"]

    csv_res = client.get(f"/api/runs/{sample_run_id}/export?format=csv")
    assert csv_res.status_code == 200
    assert csv_res.headers["content-type"].startswith("text/csv")
    lines = csv_res.text.strip().splitlines()
    assert lines[0].startswith("source_table,source_column,target_table,target_column")
    assert len(lines) == 54  # 表头 + 53 条边


def test_pages_render(client, sample_run_id):
    home = client.get("/")
    assert home.status_code == 200
    assert "改字段之前" in home.text
    assert "ods_orders.amount" in home.text

    workbench = client.get("/workbench")
    assert workbench.status_code == 200
    assert "解析样例（12 个脚本）" in workbench.text
    assert "01_dwd_customer.sql" in workbench.text

    result = client.get(f"/result/{sample_run_id}")
    assert result.status_code == 200
    assert "按目标表分组的列级血缘" in result.text
    assert "v_loop_a -&gt; v_loop_b" in result.text or "v_loop_a -> v_loop_b" in result.text

    review = client.get("/review?table=ods_orders&column=amount&op=drop")
    assert review.status_code == 200
    assert "迁移清单" in review.text
    assert "6 步" in review.text


def test_form_main_flow(client):
    """页面主流程：提交解析表单 → 303 → 结果页。"""
    res = client.post("/runs/submit", data={"mode": "sample"}, follow_redirects=False)
    assert res.status_code == 303
    location = res.headers["location"]
    assert location.startswith("/result/run_")
    page = client.get(location)
    assert page.status_code == 200
    assert "列级血缘边" in page.text


def test_form_broken_sql_shows_position(client):
    res = client.post(
        "/runs/submit",
        data={"mode": "custom", "sql": BROKEN_SQL, "filename": "bad_form.sql"},
        follow_redirects=False,
    )
    assert res.status_code == 422
    assert "解析未通过" in res.text
    assert "括号未闭合" in res.text
    assert "第 4 行第 8 列" in res.text


def test_result_page_missing_run_is_404(client):
    res = client.get("/result/run_not_exist")
    assert res.status_code == 404
    assert "找不到运行记录" in res.text
