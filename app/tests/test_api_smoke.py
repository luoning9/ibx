from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import app


client = TestClient(app)


def test_healthz() -> None:
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_strategies() -> None:
    resp = client.get("/v1/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 0
    if data:
        assert "capabilities" in data[0]
        assert "can_delete" in data[0]["capabilities"]


def test_strategy_detail_contains_trade_type_and_symbols() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "detail schema test",
        "trade_type": "buy",
        "symbols": [{"code": "SLV", "trade_type": "buy"}],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    resp = client.get(f"/v1/strategies/{strategy_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_type"] in {"buy", "sell", "switch", "open", "close", "spread"}
    assert isinstance(body["symbols"], list)
    assert len(body["symbols"]) >= 1
    assert "code" in body["symbols"][0]
    assert "trade_type" in body["symbols"][0]
    assert "editable" in body
    assert "capabilities" in body
    assert "can_delete" in body["capabilities"]
    assert "trigger_group_status" in body


def test_create_strategy_uses_symbols_schema() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "test create with new symbols schema",
        "trade_type": "buy",
        "symbols": [{"code": "SLV", "trade_type": "buy"}],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == strategy_id
    assert body["trade_type"] == "buy"
    assert body["symbols"][0]["code"] == "SLV"


def test_create_futures_open_strategy_with_open_symbol_leg() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "test futures open leg",
        "trade_type": "open",
        "symbols": [{"code": "SIH7", "trade_type": "open"}],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_type"] == "open"
    assert body["symbols"][0]["trade_type"] == "open"


def test_reject_mixed_symbol_trade_type_for_futures_strategy() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "invalid mixed leg type for futures strategy",
        "trade_type": "open",
        "symbols": [{"code": "SIH7", "trade_type": "buy"}],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_create_strategy_accepts_volume_ratio_condition() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "pair metric volume ratio",
        "trade_type": "switch",
        "symbols": [
            {"code": "QQQ", "trade_type": "buy"},
            {"code": "SPY", "trade_type": "sell"},
            {"code": "VIX", "trade_type": "ref"},
        ],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "condition_logic": "AND",
        "conditions": [
            {
                "condition_type": "PAIR_PRODUCTS",
                "metric": "VOLUME_RATIO",
                "trigger_mode": "LEVEL",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.1,
                "product_a": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conditions_json"][0]["metric"] == "VOLUME_RATIO"


def test_create_strategy_accepts_amount_ratio_condition() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "pair metric amount ratio",
        "trade_type": "switch",
        "symbols": [
            {"code": "QQQ", "trade_type": "buy"},
            {"code": "SPY", "trade_type": "sell"},
        ],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "condition_logic": "AND",
        "conditions": [
            {
                "condition_type": "PAIR_PRODUCTS",
                "metric": "AMOUNT_RATIO",
                "trigger_mode": "LEVEL",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": "<=",
                "value": 0.95,
                "product_a": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conditions_json"][0]["metric"] == "AMOUNT_RATIO"


def test_create_strategy_maps_legacy_liquidity_ratio_to_volume_ratio() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "legacy liquidity ratio compatibility",
        "trade_type": "switch",
        "symbols": [
            {"code": "QQQ", "trade_type": "buy"},
            {"code": "SPY", "trade_type": "sell"},
        ],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "condition_logic": "AND",
        "conditions": [
            {
                "condition_type": "PAIR_PRODUCTS",
                "metric": "LIQUIDITY_RATIO",
                "trigger_mode": "LEVEL",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product_a": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conditions_json"][0]["metric"] == "VOLUME_RATIO"


def test_reject_invalid_metric_for_condition_type() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "invalid metric for single product",
        "trade_type": "buy",
        "symbols": [{"code": "SLV", "trade_type": "buy"}],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "condition_logic": "AND",
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "AMOUNT_RATIO",
                "trigger_mode": "LEVEL",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "SLV",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_reject_invalid_trigger_rule_for_amount_ratio() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "invalid trigger for amount ratio",
        "trade_type": "switch",
        "symbols": [
            {"code": "QQQ", "trade_type": "buy"},
            {"code": "SPY", "trade_type": "sell"},
        ],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "condition_logic": "AND",
        "conditions": [
            {
                "condition_type": "PAIR_PRODUCTS",
                "metric": "AMOUNT_RATIO",
                "trigger_mode": "CROSS_UP",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product_a": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_reject_minute_window_for_volume_ratio() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    payload = {
        "id": strategy_id,
        "description": "invalid minute window for volume ratio",
        "trade_type": "switch",
        "symbols": [
            {"code": "QQQ", "trade_type": "buy"},
            {"code": "SPY", "trade_type": "sell"},
        ],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "condition_logic": "AND",
        "conditions": [
            {
                "condition_type": "PAIR_PRODUCTS",
                "metric": "VOLUME_RATIO",
                "trigger_mode": "LEVEL",
                "evaluation_window": "5m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product_a": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_downstream_can_only_have_one_upstream() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:8].upper()}"
    upstream_a_id = f"S-UT-{uuid4().hex[:8].upper()}"
    upstream_b_id = f"S-UT-{uuid4().hex[:8].upper()}"

    create_payloads = [
        {
            "id": downstream_id,
            "description": "downstream target",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
        {
            "id": upstream_a_id,
            "description": "upstream a",
            "trade_type": "buy",
            "symbols": [{"code": "GLD", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
        {
            "id": upstream_b_id,
            "description": "upstream b",
            "trade_type": "buy",
            "symbols": [{"code": "QQQ", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    ]
    for payload in create_payloads:
        created = client.post("/v1/strategies", json=payload)
        assert created.status_code == 200

    first_link = client.put(
        f"/v1/strategies/{upstream_a_id}/actions",
        json={
            "trade_action_json": None,
            "next_strategy_id": downstream_id,
            "next_strategy_note": "link from upstream a",
        },
    )
    assert first_link.status_code == 200

    downstream_detail = client.get(f"/v1/strategies/{downstream_id}")
    assert downstream_detail.status_code == 200
    assert downstream_detail.json()["upstream_strategy"]["id"] == upstream_a_id

    second_link = client.put(
        f"/v1/strategies/{upstream_b_id}/actions",
        json={
            "trade_action_json": None,
            "next_strategy_id": downstream_id,
            "next_strategy_note": "link from upstream b",
        },
    )
    assert second_link.status_code == 422

    unlink = client.put(
        f"/v1/strategies/{upstream_a_id}/actions",
        json={
            "trade_action_json": None,
            "next_strategy_id": None,
            "next_strategy_note": None,
        },
    )
    assert unlink.status_code == 200

    downstream_detail_after_unlink = client.get(f"/v1/strategies/{downstream_id}")
    assert downstream_detail_after_unlink.status_code == 200
    assert downstream_detail_after_unlink.json()["upstream_strategy"] is None


def test_delete_strategy_uses_soft_delete_and_hides_strategy() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    create_payload = {
        "id": strategy_id,
        "description": "soft delete target",
        "trade_type": "buy",
        "symbols": [{"code": "SLV", "trade_type": "buy"}],
        "currency": "USD",
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    created = client.post("/v1/strategies", json=create_payload)
    assert created.status_code == 200

    deleted = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "deleted"

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 404

    listed = client.get("/v1/strategies")
    assert listed.status_code == 200
    listed_ids = {row["id"] for row in listed.json()}
    assert strategy_id not in listed_ids

    deleted_again = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted_again.status_code == 200
    assert deleted_again.json()["message"] == "already_deleted"


def test_delete_upstream_strategy_clears_downstream_upstream_pointer() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:8].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:8].upper()}"

    for payload in (
        {
            "id": downstream_id,
            "description": "downstream",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
        {
            "id": upstream_id,
            "description": "upstream",
            "trade_type": "buy",
            "symbols": [{"code": "GLD", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    ):
        created = client.post("/v1/strategies", json=payload)
        assert created.status_code == 200

    linked = client.put(
        f"/v1/strategies/{upstream_id}/actions",
        json={"trade_action_json": None, "next_strategy_id": downstream_id, "next_strategy_note": None},
    )
    assert linked.status_code == 200

    deleted_upstream = client.delete(f"/v1/strategies/{upstream_id}")
    assert deleted_upstream.status_code == 200

    downstream_detail = client.get(f"/v1/strategies/{downstream_id}")
    assert downstream_detail.status_code == 200
    assert downstream_detail.json()["upstream_strategy"] is None


def test_delete_paused_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    created = client.post(
        "/v1/strategies",
        json={
            "id": strategy_id,
            "description": "paused delete guard",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    )
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'PAUSED' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_delete"] is False
    assert "PAUSED" in (detail.json()["capability_reasons"]["can_delete"] or "")

    deleted = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted.status_code == 409
    assert "PAUSED" in deleted.json()["detail"]


def test_delete_active_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    created = client.post(
        "/v1/strategies",
        json={
            "id": strategy_id,
            "description": "active delete guard",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    )
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'ACTIVE' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_delete"] is False
    assert "ACTIVE" in (detail.json()["capability_reasons"]["can_delete"] or "")

    deleted = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted.status_code == 409
    assert "ACTIVE" in deleted.json()["detail"]


def test_delete_strategy_with_active_trade_instruction_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:8].upper()}"
    created = client.post(
        "/v1/strategies",
        json={
            "id": strategy_id,
            "description": "active trade delete guard",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    )
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
              trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"T-{uuid4().hex[:8].upper()}",
                strategy_id,
                "pending instruction",
                "ORDER_SUBMITTED",
                None,
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_delete"] is False
    assert "交易未终止" in (detail.json()["capability_reasons"]["can_delete"] or "")

    deleted = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted.status_code == 409
    assert "交易未终止" in deleted.json()["detail"]


def test_delete_strategy_with_upstream_is_rejected() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:8].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:8].upper()}"

    for payload in (
        {
            "id": downstream_id,
            "description": "downstream delete guard",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
        {
            "id": upstream_id,
            "description": "upstream link",
            "trade_type": "buy",
            "symbols": [{"code": "GLD", "trade_type": "buy"}],
            "currency": "USD",
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    ):
        created = client.post("/v1/strategies", json=payload)
        assert created.status_code == 200

    linked = client.put(
        f"/v1/strategies/{upstream_id}/actions",
        json={"trade_action_json": None, "next_strategy_id": downstream_id, "next_strategy_note": None},
    )
    assert linked.status_code == 200

    detail = client.get(f"/v1/strategies/{downstream_id}")
    assert detail.status_code == 200
    assert detail.json()["upstream_strategy"]["id"] == upstream_id
    assert detail.json()["capabilities"]["can_delete"] is False
    assert "上游策略" in (detail.json()["capability_reasons"]["can_delete"] or "")

    deleted = client.delete(f"/v1/strategies/{downstream_id}")
    assert deleted.status_code == 409
    assert "上游策略" in deleted.json()["detail"]
