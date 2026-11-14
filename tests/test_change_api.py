"""整组变更演练 API：创建（幂等）、详情、两阶段切换、校验错误与页面。"""
from __future__ import annotations


def _payload(run_id):
    return {
        "run_id": run_id,
        "changes": [
            {"table": "ods_products", "action": "add_column", "new_name": "brand_code", "new_type": "varchar(32)"},
            {"table": "ods_products", "action": "rename_column", "column": "sku", "new_name": "product_sku"},
            {"table": "ods_orders", "action": "alter_type", "column": "amount", "new_type": "decimal(18,4)"},
        ],
    }


def test_create_change_session_is_idempotent(client, sample_run_id):
    payload = _payload(sample_run_id)
    first = client.post("/api/change-sessions", json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ready"] is True
    assert body["status"] == "draft"
    assert body["affected_object_count"] == 6
    # 星号对象必须被识别出来
    assert set(body["star_objects"]) == {"v_product_sales", "dws_order_wide"}
    assert set(body["rebuild_views"]) == {"v_product_sales", "v_channel_sales"}
    assert len(body["backfill_tables"]) == 4
    assert body["replay"] == {"ok": True, "parsed_count": 12, "failed_count": 0, "total_count": 12}

    second = client.post("/api/change-sessions", json=payload).json()
    assert second["reused"] is True
    assert second["session_id"] == body["session_id"]
    assert second["fingerprint"] == body["fingerprint"]


def test_change_session_detail_contains_star_and_rename_chain(client, sample_run_id):
    sid = client.post("/api/change-sessions", json=_payload(sample_run_id)).json()["session_id"]
    plan = client.get(f"/api/change-sessions/{sid}").json()["plan"]

    # 新增列经星号透传到 v_product_sales
    add = plan["add_propagation"][0]
    assert add["new_column"] == "brand_code"
    assert add["downstream_chain"][0]["object"] == "v_product_sales"
    assert add["downstream_chain"][0]["hits"] == ["star"]

    # 改名链给出基线旧列名 → 候选新列名
    rename = plan["rename_propagation"][0]
    node = rename["downstream_chain"][0]
    assert node["base_columns"] == ["sku"]
    assert node["candidate_columns"] == ["product_sku"]


def test_change_session_approve_then_sticky(client, sample_run_id):
    sid = client.post("/api/change-sessions", json=_payload(sample_run_id)).json()["session_id"]
    res = client.post(f"/api/change-sessions/{sid}/decision", json={"approve": True})
    assert res.status_code == 200
    assert res.json()["status"] == "applied"
    # 终态后再次决策不翻转
    again = client.post(f"/api/change-sessions/{sid}/decision", json={"approve": False}).json()
    assert again["status"] == "applied"


def test_change_session_reject_keeps_base(client, sample_run_id):
    payload = {
        "run_id": sample_run_id,
        "changes": [{"table": "ods_orders", "action": "add_column", "new_name": "ref_tag", "new_type": "varchar(8)"}],
    }
    sid = client.post("/api/change-sessions", json=payload).json()["session_id"]
    res = client.post(f"/api/change-sessions/{sid}/decision", json={"approve": False}).json()
    assert res["status"] == "rejected"
    assert "保留原结构" in res["rollback"]
    plan = client.get(f"/api/change-sessions/{sid}").json()["plan"]
    assert "ref_tag" not in plan["base_schemas"]["ods_orders"]


def test_failed_replay_cannot_be_applied(client, sample_run_id):
    # 用自定义脚本制造对 amount 的显式引用，再改名 → 候选重放失败
    sql = "CREATE TABLE t_amt AS SELECT o.amount AS amount FROM ods_orders o;"
    rid = client.post("/api/runs", json={"mode": "custom", "sql": sql, "filename": "t_amt.sql"}).json()["run_id"]
    payload = {
        "run_id": rid,
        "changes": [
            {"table": "ods_orders", "action": "rename_column", "column": "amount", "new_name": "order_amount"}
        ],
    }
    created = client.post("/api/change-sessions", json=payload).json()
    assert created["ready"] is False
    assert created["replay"]["failed_count"] == 1
    decision = client.post(
        f"/api/change-sessions/{created['session_id']}/decision", json={"approve": True}
    ).json()
    assert decision["status"] == "rejected"
    assert "保留原结构" in decision["rollback"]


def test_change_session_validation_errors(client, sample_run_id):
    # 缺 changes
    r1 = client.post("/api/change-sessions", json={"run_id": sample_run_id})
    assert r1.status_code == 400
    # 非法动作
    r2 = client.post(
        "/api/change-sessions",
        json={"run_id": sample_run_id, "changes": [{"table": "ods_orders", "action": "drop"}]},
    )
    assert r2.status_code == 400
    # 业务校验：未知表
    r3 = client.post(
        "/api/change-sessions",
        json={"run_id": sample_run_id, "changes": [{"table": "ghost", "action": "add_column", "new_name": "x"}]},
    )
    assert r3.status_code == 400 and "ghost" in r3.json()["detail"]
    # 未知 run
    r4 = client.post(
        "/api/change-sessions",
        json={"run_id": "run_none", "changes": [{"table": "ods_orders", "action": "add_column", "new_name": "x"}]},
    )
    assert r4.status_code == 404


def test_change_session_list_and_404(client, sample_run_id):
    sid = client.post("/api/change-sessions", json=_payload(sample_run_id)).json()["session_id"]
    listing = client.get("/api/change-sessions").json()["sessions"]
    assert any(s["session_id"] == sid for s in listing)
    assert client.get("/api/change-sessions/chg_missing").status_code == 404
    assert client.post("/api/change-sessions/chg_missing/decision", json={"approve": True}).status_code == 404


def test_rehearsal_page_renders(client):
    page = client.get("/rehearsal")
    assert page.status_code == 200
    assert "整组源表变更演练" in page.text
    assert "/rehearsal" in page.text
