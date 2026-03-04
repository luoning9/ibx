import json
import re
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import get_connection
from app.ib_data_service import FixtureBrokerDataProvider
from app.ib_trade_service import IBTradeServiceError
from app.market_data import HistoricalBar, HistoricalBarsResult
from app.main import app


client = TestClient(app)


def _ready_strategy_payload(strategy_id: str, description: str) -> dict[str, object]:
    return {
        "id": strategy_id,
        "description": description,
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": None}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
            }
        ],
        "trade_action_json": {
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 1,
        },
    }


def test_healthz() -> None:
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_condition_rules() -> None:
    resp = client.get("/v1/condition-rules")
    assert resp.status_code == 200
    body = resp.json()
    assert "trigger_mode_windows" in body
    assert "metric_trigger_operator_rules" in body
    assert "allowed_windows" in body["metric_trigger_operator_rules"]
    assert "allowed_rules" in body["metric_trigger_operator_rules"]
    assert "PRICE" in body["metric_trigger_operator_rules"]["allowed_rules"]


def test_markets() -> None:
    resp = client.get("/v1/markets")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    assert any(row["market"] == "US_STOCK" for row in rows)
    first = rows[0]
    assert "market" in first
    assert "sec_type" in first
    assert "exchange" in first
    assert "currency" in first
    assert "allowed_trade_types" in first


def test_system_status() -> None:
    resp = client.get("/v1/system-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gateway"]["trading_mode"] in {"paper", "live"}
    assert body["gateway"]["api_port"] > 0
    assert body["worker"]["enabled"] in {True, False}
    assert body["worker"]["running"] in {True, False}
    assert body["worker"]["configured_threads"] >= 1
    assert body["worker"]["live_threads"] >= 0
    assert body["worker"]["queue_length"] >= 0
    assert body["worker"]["queue_maxsize"] >= 1
    assert "broker_data" in body["providers"]
    assert "market_data" in body["providers"]
    assert body["providers"]["broker_data"]["configured"] in {"ib", "fixture"}
    assert body["providers"]["market_data"]["configured"] in {"ib", "fixture"}


def test_market_data_probe_endpoint(monkeypatch) -> None:
    utc = timezone.utc

    class _FakeProvider:
        def get_historical_bars(self, request):  # type: ignore[no-untyped-def]
            _ = request
            return HistoricalBarsResult(
                bars=[
                    HistoricalBar(
                        ts=datetime(2026, 2, 25, 14, 30, tzinfo=utc),
                        open=10.0,
                        high=10.5,
                        low=9.8,
                        close=10.2,
                        volume=1000.0,
                    )
                ],
                meta={"source": "TEST", "has_gaps": False},
            )

    monkeypatch.setattr("app.api._resolve_market_data_provider_for_probe", lambda: _FakeProvider())
    resp = client.post(
        "/v1/market-data/probe",
        json={
            "code": "SLV",
            "market": "US_STOCK",
            "start_time": "2026-02-25T14:00:00Z",
            "end_time": "2026-02-25T14:35:00Z",
            "bar_size": "1 min",
            "what_to_show": "TRADES",
            "use_rth": True,
            "include_partial_bar": True,
            "max_bars": 3,
            "page_size": 500,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_class"] == "_FakeProvider"
    assert body["request"]["contract"]["code"] == "SLV"
    assert body["meta"]["source"] == "TEST"
    assert len(body["bars"]) == 1
    assert body["bars"][0]["close"] == 10.2


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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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


def test_generate_strategy_description_by_id_api() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "",
        "market": "COMEX_FUTURES",
        "trade_type": "spread",
        "symbols": [
            {"code": "SIH7", "trade_type": "open"},
            {"code": "SIK7", "trade_type": "close"},
        ],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "operator": "<=",
                "value": 30.0,
                "product": "SIH7",
            }
        ],
        "trade_action_json": {
            "action_type": "FUT_ROLL",
            "close_contract": "SIH7",
            "open_contract": "SIK7",
            "close_order_type": "LMT",
            "open_order_type": "MKT",
            "quantity": 2,
        },
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    resp = client.get(f"/v1/strategies/{strategy_id}/description/generate")
    assert resp.status_code == 200
    body = resp.json()
    assert "description" in body
    assert "COMEX期货" in body["description"]
    assert "开仓 SIH7，平仓 SIK7" in body["description"]
    assert "触发条件：" in body["description"]
    assert "SIH7 PRICE LEVEL_INSTANT <=" in body["description"]
    assert "下单：展期 SIH7->SIK7 数量 2 手" in body["description"]


def test_create_strategy_generates_short_system_id_when_missing_id() -> None:
    payload = {
        "description": "auto id strategy",
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
    assert re.fullmatch(r"S-[0-9A-F]{4}", str(body["id"])) is not None


def test_copy_strategy_creates_new_strategy_with_source_markers() -> None:
    source_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(source_id, "copy source strategy"))
    assert created.status_code == 200

    copied = client.post(f"/v1/strategies/{source_id}/copy")
    assert copied.status_code == 200
    body = copied.json()

    assert body["id"] != source_id
    assert source_id in body["description"]
    assert "复制而来" in body["description"]
    assert body["status"] == "PENDING_ACTIVATION"
    assert body["trade_type"] == "buy"
    assert len(body["symbols"]) == 1
    assert body["trade_action_json"] is not None

    created_events = [item for item in body["events"] if item["event_type"] == "CREATED"]
    assert len(created_events) >= 1
    assert source_id in created_events[0]["detail"]
    assert "复制而来" in created_events[0]["detail"]


def test_recover_trade_instruction_reconcile_by_order_ref(monkeypatch) -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    trade_id = f"T-{uuid4().hex[:10].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "recover reconcile"))
    assert created.status_code == 200
    trade_action = created.json()["trade_action_json"]
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with get_connection() as conn:
        conn.execute(
            "UPDATE strategies SET status = 'TRIGGERED', updated_at = ? WHERE id = ?",
            (now_iso, strategy_id),
        )
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trade_id, strategy_id, "STOCK_TRADE BUY AAPL MKT qty=1", "ORDER_DISPATCHING", None, now_iso),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                strategy_id,
                None,
                "ORDER_DISPATCHING",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"trade_action": trade_action, "dispatch": {"order_ref": trade_id}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    class _FakeTradeService:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            _ = (args, kwargs)

        def poll_order_status(self, *, order_id=None, perm_id=None):  # type: ignore[no-untyped-def]
            _ = (order_id, perm_id)
            return None

        def poll_order_status_by_order_ref(self, *, order_ref):  # type: ignore[no-untyped-def]
            assert order_ref == trade_id
            return SimpleNamespace(
                order_id=31001,
                perm_id=910001,
                status="Submitted",
                normalized_status="ORDER_SUBMITTED",
                terminal=False,
                filled_qty=0.0,
                remaining_qty=1.0,
                avg_fill_price=None,
                error_message=None,
            )

    monkeypatch.setattr("app.store.IBTradeService", _FakeTradeService)
    recovered = client.post(
        f"/v1/trade-instructions/{trade_id}/recover",
        json={"action": "reconcile"},
    )
    assert recovered.status_code == 200
    body = recovered.json()
    assert body["trade_id"] == trade_id
    assert body["strategy_id"] == strategy_id
    assert body["trade_status"] == "ORDER_SUBMITTED"
    assert body["strategy_status"] == "ORDER_SUBMITTED"
    assert body["perm_id"] == 910001
    assert body["ib_order_id"] == "910001"

    with get_connection() as conn:
        instruction_row = conn.execute(
            "SELECT status FROM trade_instructions WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        assert instruction_row is not None
        assert instruction_row["status"] == "ORDER_SUBMITTED"
        order_row = conn.execute(
            "SELECT status, ib_order_id FROM orders WHERE id = ?",
            (trade_id,),
        ).fetchone()
        assert order_row is not None
        assert order_row["status"] == "ORDER_SUBMITTED"
        assert order_row["ib_order_id"] == "910001"


def test_recover_trade_instruction_mark_failed() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    trade_id = f"T-{uuid4().hex[:10].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "recover fail"))
    assert created.status_code == 200
    trade_action = created.json()["trade_action_json"]
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with get_connection() as conn:
        conn.execute(
            "UPDATE strategies SET status = 'TRIGGERED', updated_at = ? WHERE id = ?",
            (now_iso, strategy_id),
        )
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trade_id, strategy_id, "STOCK_TRADE BUY AAPL MKT qty=1", "ORDER_DISPATCHING", None, now_iso),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                strategy_id,
                None,
                "ORDER_DISPATCHING",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"trade_action": trade_action, "dispatch": {"order_ref": trade_id}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    recovered = client.post(
        f"/v1/trade-instructions/{trade_id}/recover",
        json={"action": "mark_failed", "reason": "manual intervention"},
    )
    assert recovered.status_code == 200
    body = recovered.json()
    assert body["trade_status"] == "FAILED"
    assert body["strategy_status"] == "FAILED"

    with get_connection() as conn:
        strategy_row = conn.execute(
            "SELECT status FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        assert strategy_row is not None
        assert strategy_row["status"] == "FAILED"


def test_completed_trade_instructions_recent_returns_terminal_within_week() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    recent_trade_id = f"T-{uuid4().hex[:10].upper()}"
    old_trade_id = f"T-{uuid4().hex[:10].upper()}"
    open_trade_id = f"T-{uuid4().hex[:10].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "completed recent"))
    assert created.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat().replace("+00:00", "Z")
    old_iso = (now - timedelta(days=8)).isoformat().replace("+00:00", "Z")

    with get_connection() as conn:
        conn.execute(
            "UPDATE strategies SET status = 'ORDER_SUBMITTED', updated_at = ? WHERE id = ?",
            (now_iso, strategy_id),
        )
        conn.executemany(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (recent_trade_id, strategy_id, "RECENT FILLED", "FILLED", None, now_iso),
                (old_trade_id, strategy_id, "OLD FAILED", "FAILED", None, old_iso),
                (open_trade_id, strategy_id, "OPEN SUBMITTED", "ORDER_SUBMITTED", None, now_iso),
            ],
        )
        conn.executemany(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    recent_trade_id,
                    strategy_id,
                    "910101",
                    "FILLED",
                    1.0,
                    100.2,
                    1.0,
                    None,
                    json.dumps({"dispatch": {"order_ref": recent_trade_id}}),
                    now_iso,
                    now_iso,
                ),
                (
                    old_trade_id,
                    strategy_id,
                    "910102",
                    "FAILED",
                    1.0,
                    None,
                    0.0,
                    "rejected",
                    json.dumps({"dispatch": {"order_ref": old_trade_id}}),
                    old_iso,
                    old_iso,
                ),
                (
                    open_trade_id,
                    strategy_id,
                    "910103",
                    "ORDER_SUBMITTED",
                    1.0,
                    None,
                    0.0,
                    None,
                    json.dumps({"dispatch": {"order_ref": open_trade_id}}),
                    now_iso,
                    now_iso,
                ),
            ],
        )
        conn.commit()

    resp = client.get("/v1/trade-instructions/completed-recent")
    assert resp.status_code == 200
    rows = resp.json()
    trade_ids = {row["trade_id"] for row in rows}
    assert recent_trade_id in trade_ids
    assert old_trade_id not in trade_ids
    assert open_trade_id not in trade_ids


def test_trade_instruction_orders_and_active_counts() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    trade_id = f"T-{uuid4().hex[:10].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "trade order list"))
    assert created.status_code == 200

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trade_id, strategy_id, "FUT_ROLL SIH6->SIK6 qty=2", "ORDER_SUBMITTED", None, now_iso),
        )
        conn.executemany(
            """
            INSERT INTO orders (
                id, trade_id, strategy_id, leg_role, sequence_no, ib_order_id, status, qty,
                avg_fill_price, filled_qty, error_message, order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"{trade_id}-L1",
                    trade_id,
                    strategy_id,
                    "ROLL_CLOSE",
                    1,
                    "10001",
                    "FILLED",
                    2.0,
                    31.2,
                    2.0,
                    None,
                    json.dumps({"leg": "close"}),
                    now_iso,
                    now_iso,
                ),
                (
                    f"{trade_id}-L2",
                    trade_id,
                    strategy_id,
                    "ROLL_OPEN",
                    2,
                    "10002",
                    "ORDER_SUBMITTED",
                    2.0,
                    None,
                    0.0,
                    None,
                    json.dumps({"leg": "open"}),
                    now_iso,
                    now_iso,
                ),
            ],
        )
        conn.commit()

    active_resp = client.get("/v1/trade-instructions/active")
    assert active_resp.status_code == 200
    active_rows = active_resp.json()
    hit = next((row for row in active_rows if row["trade_id"] == trade_id), None)
    assert hit is not None
    assert hit["order_count"] == 2
    assert hit["filled_order_count"] == 1
    assert hit["perm_id"] == 10001

    orders_resp = client.get(f"/v1/trade-instructions/{trade_id}/orders")
    assert orders_resp.status_code == 200
    order_rows = orders_resp.json()
    assert len(order_rows) == 2
    assert [row["sequence_no"] for row in order_rows] == [1, 2]
    assert [row["leg_role"] for row in order_rows] == ["ROLL_CLOSE", "ROLL_OPEN"]


def test_trade_logs_supports_trade_id_filter() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    target_trade_id = f"T-{uuid4().hex[:10].upper()}"
    other_trade_id = f"T-{uuid4().hex[:10].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "trade logs filter"))
    assert created.status_code == 200

    now = datetime.now(timezone.utc).replace(microsecond=0)
    older = now - timedelta(minutes=1)
    now_iso = now.isoformat().replace("+00:00", "Z")
    older_iso = older.isoformat().replace("+00:00", "Z")

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO trade_logs (
                timestamp, strategy_id, trade_id, stage, result, detail
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (older_iso, strategy_id, target_trade_id, "verify", "PASSED", "target-older"),
                (now_iso, strategy_id, target_trade_id, "dispatch", "ORDER_SUBMITTED", "target-newer"),
                (now_iso, strategy_id, other_trade_id, "verify", "PASSED", "other-row"),
            ],
        )
        conn.commit()

    resp = client.get("/v1/trade-logs", params={"trade_id": target_trade_id})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert all(row["trade_id"] == target_trade_id for row in rows)
    assert [row["detail"] for row in rows] == ["target-newer", "target-older"]


def test_other_open_orders_excludes_active_trade_instruction_perm_ids(monkeypatch) -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    trade_id = f"T-{uuid4().hex[:10].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "other open orders"))
    assert created.status_code == 200
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with get_connection() as conn:
        conn.execute(
            "UPDATE strategies SET status = 'TRIGGERED', updated_at = ? WHERE id = ?",
            (now_iso, strategy_id),
        )
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trade_id, strategy_id, "STOCK_TRADE BUY AAPL MKT qty=1", "ORDER_SUBMITTED", None, now_iso),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                strategy_id,
                "910001",
                "ORDER_SUBMITTED",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"dispatch": {"order_ref": trade_id}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    class _FakeTradeService:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            _ = (args, kwargs)
            self.client_id = 96

        def list_active_orders(self):  # type: ignore[no-untyped-def]
            now = datetime.now(timezone.utc)
            return [
                SimpleNamespace(
                    updated_at=now,
                    perm_id=910001,
                    order_id=31001,
                    client_id=96,
                    symbol="AAPL",
                    sec_type="STK",
                    side="BUY",
                    order_type="MKT",
                    quantity=1.0,
                    status="Submitted",
                    normalized_status="ORDER_SUBMITTED",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    avg_fill_price=None,
                    account_code="DU123",
                ),
                SimpleNamespace(
                    updated_at=None,
                    perm_id=920002,
                    order_id=31002,
                    client_id=96,
                    symbol="TSLA",
                    sec_type="STK",
                    side="SELL",
                    order_type="LMT",
                    quantity=2.0,
                    status="Submitted",
                    normalized_status="ORDER_SUBMITTED",
                    filled_qty=0.0,
                    remaining_qty=2.0,
                    avg_fill_price=None,
                    account_code="DU123",
                ),
            ]

    monkeypatch.setattr("app.store.IBTradeService", _FakeTradeService)
    resp = client.get("/v1/trade-instructions/open-orders/others")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["perm_id"] == 920002
    assert rows[0]["symbol"] == "TSLA"
    assert rows[0]["updated_at"] is None
    assert rows[0]["can_cancel"] is True


def test_cancel_other_open_order_by_perm_id(monkeypatch) -> None:
    class _FakeTradeService:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            _ = (args, kwargs)

        def cancel_order(self, *, perm_id=None, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs.get("wait_for_terminal") is True
            assert kwargs.get("timeout_seconds") == 5.0
            assert perm_id == 920002
            return SimpleNamespace(
                order_id=31002,
                perm_id=920002,
                status="Cancelled",
                normalized_status="CANCELLED",
                terminal=True,
                updated_at=datetime.now(timezone.utc),
            )

    monkeypatch.setattr("app.store.IBTradeService", _FakeTradeService)
    resp = client.post("/v1/trade-instructions/open-orders/920002/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["perm_id"] == 920002
    assert body["order_id"] == 31002
    assert body["status"] == "CANCELLED"
    assert body["terminal"] is True
    assert "cancel requested" in body["message"]


def test_cancel_other_open_order_returns_conflict_on_client_id_mismatch(monkeypatch) -> None:
    class _FakeTradeService:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            _ = (args, kwargs)

        def cancel_order(self, *, perm_id=None, **kwargs):  # type: ignore[no-untyped-def]
            _ = (perm_id, kwargs)
            raise IBTradeServiceError(
                "cancelOrder rejected: order belongs to a different clientId "
                "order_client_id=0 current_client_id=96"
            )

    monkeypatch.setattr("app.store.IBTradeService", _FakeTradeService)
    resp = client.post("/v1/trade-instructions/open-orders/920002/cancel")
    assert resp.status_code == 409
    assert "different clientId" in str(resp.json().get("detail", ""))


def test_trade_action_enriched_with_strategy_market_and_account() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = _ready_strategy_payload(strategy_id, "trade action enrichment")
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200
    body = created.json()
    action = body["trade_action_json"]
    assert action["market"] == body["market"]
    assert "account_code" in action

    updated = client.put(
        f"/v1/strategies/{strategy_id}/actions",
        json={
            "trade_action_json": {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 2,
            },
            "next_strategy_id": None,
            "next_strategy_note": None,
        },
    )
    assert updated.status_code == 200
    updated_body = updated.json()
    updated_action = updated_body["trade_action_json"]
    assert updated_action["market"] == updated_body["market"]
    assert "account_code" in updated_action


def test_create_strategy_market_mapping_fields() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "market mapping test",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy"}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["market"] == "US_STOCK"
    assert body["sec_type"] == "STK"
    assert body["exchange"] == "SMART"


def test_reject_market_trade_type_mismatch() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "market trade type mismatch",
        "market": "US_STOCK",
        "trade_type": "open",
        "symbols": [{"code": "SIH7", "trade_type": "open"}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_symbols_return_contract_id_nullable() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "symbols contract id nullable",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": None}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbols"][0]["contract_id"] is None


def test_activate_moves_to_verifying() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "activate verify flow",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": None}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
            }
        ],
        "trade_action_json": {
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 1,
        },
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    activated = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "VERIFYING"

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "VERIFYING"

    events = client.get(f"/v1/strategies/{strategy_id}/events")
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()]
    assert "VERIFYING" in event_types
    assert "ACTIVATED" not in event_types


def test_activate_does_not_verify_inline_when_market_mapping_invalid() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "verify fail flow",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": None}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
            }
        ],
        "trade_action_json": {
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 1,
        },
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute("UPDATE strategies SET market = 'INVALID_MARKET' WHERE id = ?", (strategy_id,))
        conn.commit()

    activated = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "VERIFYING"

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "VERIFYING"


def test_pause_resume_when_verifying() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "pause resume in verifying",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": None}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
            }
        ],
        "trade_action_json": {
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 1,
        },
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    activated = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "VERIFYING"

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "VERIFYING"
    assert detail.json()["capabilities"]["can_pause"] is True

    paused = client.post(f"/v1/strategies/{strategy_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"

    resumed = client.post(f"/v1/strategies/{strategy_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "VERIFYING"

    detail_after = client.get(f"/v1/strategies/{strategy_id}")
    assert detail_after.status_code == 200
    assert detail_after.json()["status"] == "VERIFYING"


def test_activate_enqueues_strategy_immediately(monkeypatch) -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "activate enqueue"))
    assert created.status_code == 200

    calls: list[dict[str, object]] = []

    def _fake_enqueue(strategy_id_arg: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"strategy_id": strategy_id_arg, "kwargs": dict(kwargs)})
        return True

    monkeypatch.setattr("app.api.worker_engine.enqueue_strategy", _fake_enqueue)

    activated = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert activated.status_code == 200
    assert len(calls) == 1
    assert calls[0]["strategy_id"] == strategy_id
    assert calls[0]["kwargs"]["reason"] == "api_activate"


def test_resume_enqueues_strategy_immediately(monkeypatch) -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "resume enqueue"))
    assert created.status_code == 200
    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'PAUSED' WHERE id = ?", (strategy_id,))
        conn.commit()

    calls: list[dict[str, object]] = []

    def _fake_enqueue(strategy_id_arg: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"strategy_id": strategy_id_arg, "kwargs": dict(kwargs)})
        return True

    monkeypatch.setattr("app.api.worker_engine.enqueue_strategy", _fake_enqueue)

    resumed = client.post(f"/v1/strategies/{strategy_id}/resume")
    assert resumed.status_code == 200
    assert len(calls) == 1
    assert calls[0]["strategy_id"] == strategy_id
    assert calls[0]["kwargs"]["reason"] == "api_resume"


def test_activate_rejected_when_strategy_locked() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json=_ready_strategy_payload(strategy_id, "activate blocked by lock"),
    )
    assert created.status_code == 200

    lock_until = "2099-01-01T00:00:00Z"
    with get_connection() as conn:
        conn.execute(
            "UPDATE strategies SET lock_until = ? WHERE id = ?",
            (lock_until, strategy_id),
        )
        conn.commit()

    resp = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "STRATEGY_LOCKED"
    assert body["detail"]["action"] == "activate"
    assert body["detail"]["lock_until"] == lock_until


def test_pause_rejected_when_strategy_locked() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json=_ready_strategy_payload(strategy_id, "pause blocked by lock"),
    )
    assert created.status_code == 200
    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'ACTIVE' WHERE id = ?", (strategy_id,))
        conn.execute(
            "UPDATE strategies SET lock_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00Z", strategy_id),
        )
        conn.commit()

    resp = client.post(f"/v1/strategies/{strategy_id}/pause")
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "STRATEGY_LOCKED"
    assert body["detail"]["action"] == "pause"


def test_cancel_verifying_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json=_ready_strategy_payload(strategy_id, "cancel blocked in verifying"),
    )
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'VERIFYING' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_cancel"] is False
    assert "VERIFYING" in (detail.json()["capability_reasons"]["can_cancel"] or "")

    cancelled = client.post(f"/v1/strategies/{strategy_id}/cancel")
    assert cancelled.status_code == 409
    assert "VERIFYING" in cancelled.json()["detail"]


def test_cancel_triggered_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json=_ready_strategy_payload(strategy_id, "cancel blocked in triggered"),
    )
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'TRIGGERED' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_cancel"] is False
    assert "TRIGGERED" in (detail.json()["capability_reasons"]["can_cancel"] or "")

    cancelled = client.post(f"/v1/strategies/{strategy_id}/cancel")
    assert cancelled.status_code == 409
    assert "TRIGGERED" in cancelled.json()["detail"]


def test_cancel_order_submitted_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json=_ready_strategy_payload(strategy_id, "cancel blocked in order submitted"),
    )
    assert created.status_code == 200

    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'ORDER_SUBMITTED' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_cancel"] is False
    assert "ORDER_SUBMITTED" in (detail.json()["capability_reasons"]["can_cancel"] or "")

    cancelled = client.post(f"/v1/strategies/{strategy_id}/cancel")
    assert cancelled.status_code == 409
    assert "ORDER_SUBMITTED" in cancelled.json()["detail"]


def test_portfolio_summary_uses_broker_data_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.store.get_broker_data_provider",
        lambda: FixtureBrokerDataProvider(),
    )
    resp = client.get("/v1/portfolio-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["net_liquidation"] == 143445.18
    assert body["available_funds"] == 0.0
    assert body["unrealized_pnl"] == 9159.22
    assert body["realized_pnl"] == 0.0
    assert body["daily_pnl"] == 9159.22
    assert body["updated_at"] == "2026-01-01T00:00:00Z"


def test_positions_uses_broker_data_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.store.get_broker_data_provider",
        lambda: FixtureBrokerDataProvider(),
    )
    resp = client.get("/v1/positions")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 9
    assert rows[0]["symbol"] == "CRML"
    assert rows[0]["sec_type"] == "STK"
    assert rows[0]["position_unit"] == "股"
    assert rows[0]["updated_at"] == "2026-01-01T00:00:00Z"

    one = client.get("/v1/positions", params={"symbol": "vgt"})
    assert one.status_code == 200
    one_rows = one.json()
    assert len(one_rows) == 1
    assert one_rows[0]["symbol"] == "VGT"
    assert one_rows[0]["position_qty"] == 41.0


def test_update_config_resets_status_to_pending_activation() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "reset to pending after config change",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": None}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
            }
        ],
        "trade_action_json": {
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 1,
        },
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    activated = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert activated.status_code == 200
    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'ACTIVE' WHERE id = ?", (strategy_id,))
        conn.commit()
    paused = client.post(f"/v1/strategies/{strategy_id}/pause")
    assert paused.status_code == 200

    updated = client.patch(
        f"/v1/strategies/{strategy_id}/basic",
        json={
            "description": "changed description",
            "symbols": [{"code": "MSFT", "trade_type": "buy", "contract_id": None}],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "PENDING_ACTIVATION"


def test_patch_basic_updates_logical_activated_at() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "logical activation patch"))
    assert created.status_code == 200

    logical_activated_at = "2026-02-25T14:30:00Z"
    updated = client.patch(
        f"/v1/strategies/{strategy_id}/basic",
        json={
            "logical_activated_at": logical_activated_at,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["logical_activated_at"] == logical_activated_at

    with get_connection() as conn:
        row = conn.execute(
            "SELECT logical_activated_at FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        assert row is not None
        assert row["logical_activated_at"] == logical_activated_at


def test_patch_basic_event_detail_contains_changed_fields() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "before-basic-event"))
    assert created.status_code == 200

    updated = client.patch(
        f"/v1/strategies/{strategy_id}/basic",
        json={
            "description": "after-basic-event",
            "symbols": [{"code": "GLD", "trade_type": "buy", "contract_id": None}],
            "upstream_only_activation": True,
        },
    )
    assert updated.status_code == 200

    events = client.get(f"/v1/strategies/{strategy_id}/events")
    assert events.status_code == 200
    basic_events = [item for item in events.json() if item["event_type"] == "BASIC_UPDATED"]
    assert len(basic_events) >= 1
    detail = str(basic_events[0]["detail"] or "")
    assert "description:" in detail
    assert "before-basic-event -> after-basic-event" in detail
    assert "symbols:" in detail
    assert "buy:AAPL -> buy:GLD" in detail
    assert "upstream_only_activation: false -> true" in detail


def test_patch_basic_with_integer_contract_id_does_not_crash() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    payload = {
        "id": strategy_id,
        "description": "before-int-contract-id",
        "market": "US_STOCK",
        "trade_type": "buy",
        "symbols": [{"code": "AAPL", "trade_type": "buy", "contract_id": 39039301}],
        "expire_mode": "relative",
        "expire_in_seconds": 86400,
        "conditions": [
            {
                "condition_type": "SINGLE_PRODUCT",
                "metric": "PRICE",
                "trigger_mode": "LEVEL_INSTANT",
                "evaluation_window": "1m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "AAPL",
            }
        ],
        "trade_action_json": {
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 1,
        },
    }
    created = client.post("/v1/strategies", json=payload)
    assert created.status_code == 200

    updated = client.patch(
        f"/v1/strategies/{strategy_id}/basic",
        json={"description": "after-int-contract-id"},
    )
    assert updated.status_code == 200

    events = client.get(f"/v1/strategies/{strategy_id}/events")
    assert events.status_code == 200
    basic_events = [item for item in events.json() if item["event_type"] == "BASIC_UPDATED"]
    assert len(basic_events) >= 1
    detail = str(basic_events[0]["detail"] or "")
    assert "symbols:" not in detail
    assert "description: before-int-contract-id -> after-int-contract-id" in detail


def test_patch_basic_from_paused_keeps_logical_activated_at() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "logical keep from paused"))
    assert created.status_code == 200

    activated = client.post(f"/v1/strategies/{strategy_id}/activate")
    assert activated.status_code == 200
    with get_connection() as conn:
        conn.execute("UPDATE strategies SET status = 'ACTIVE' WHERE id = ?", (strategy_id,))
        conn.commit()
    paused = client.post(f"/v1/strategies/{strategy_id}/pause")
    assert paused.status_code == 200

    logical_activated_at = "2026-02-25T14:30:00Z"
    updated = client.patch(
        f"/v1/strategies/{strategy_id}/basic",
        json={
            "logical_activated_at": logical_activated_at,
            "description": "changed while paused",
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["status"] == "PENDING_ACTIVATION"
    assert body["logical_activated_at"] == logical_activated_at

    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, activated_at, logical_activated_at FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "PENDING_ACTIVATION"
        assert row["activated_at"] is None
        assert row["logical_activated_at"] == logical_activated_at


def test_patch_basic_trade_type_change_auto_clears_incompatible_trade_action() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post("/v1/strategies", json=_ready_strategy_payload(strategy_id, "auto clear action"))
    assert created.status_code == 200

    updated = client.patch(
        f"/v1/strategies/{strategy_id}/basic",
        json={
            "market": "COMEX_FUTURES",
            "trade_type": "open",
            "symbols": [{"code": "GC", "trade_type": "open", "contract_id": None}],
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["trade_type"] == "open"
    assert body["trade_action_json"] is None


def test_patch_basic_trade_type_change_resets_next_mode_when_action_auto_cleared() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:4].upper()}"

    created_downstream = client.post(
        "/v1/strategies",
        json={
            "id": downstream_id,
            "description": "downstream for auto mode reset",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    )
    assert created_downstream.status_code == 200
    created_upstream = client.post(
        "/v1/strategies",
        json=_ready_strategy_payload(upstream_id, "upstream for auto mode reset"),
    )
    assert created_upstream.status_code == 200

    linked = client.put(
        f"/v1/strategies/{upstream_id}/actions",
        json={
            "trade_action_json": {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            },
            "next_strategy_id": downstream_id,
            "next_strategy_note": "auto mode reset",
            "next_strategy_activation_mode": "AFTER_TRADE_SUBMITTED",
        },
    )
    assert linked.status_code == 200

    updated = client.patch(
        f"/v1/strategies/{upstream_id}/basic",
        json={
            "market": "COMEX_FUTURES",
            "trade_type": "open",
            "symbols": [{"code": "GC", "trade_type": "open", "contract_id": None}],
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["trade_action_json"] is None
    assert body["next_strategy_activation_mode"] == "IMMEDIATE"
    assert body["next_strategy"] is not None
    assert body["next_strategy"]["id"] == downstream_id


def test_create_futures_open_strategy_with_open_symbol_leg() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
                "trigger_mode": "LEVEL_CONFIRM",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.1,
                "product": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conditions_json"][0]["metric"] == "VOLUME_RATIO"


def test_create_strategy_accepts_amount_ratio_condition() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
                "trigger_mode": "LEVEL_CONFIRM",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": "<=",
                "value": 0.95,
                "product": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conditions_json"][0]["metric"] == "AMOUNT_RATIO"


def test_create_strategy_maps_legacy_liquidity_ratio_to_volume_ratio() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
                "trigger_mode": "LEVEL_CONFIRM",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conditions_json"][0]["metric"] == "VOLUME_RATIO"


def test_reject_invalid_metric_for_condition_type() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
                "trigger_mode": "LEVEL_CONFIRM",
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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
                "trigger_mode": "CROSS_UP_INSTANT",
                "evaluation_window": "1h",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_reject_minute_window_for_volume_ratio() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
                "trigger_mode": "LEVEL_CONFIRM",
                "evaluation_window": "5m",
                "window_price_basis": "CLOSE",
                "operator": ">=",
                "value": 1.0,
                "product": "QQQ",
                "product_b": "SPY",
            }
        ],
    }
    resp = client.post("/v1/strategies", json=payload)
    assert resp.status_code == 422


def test_downstream_can_only_have_one_upstream() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_a_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_b_id = f"S-UT-{uuid4().hex[:4].upper()}"

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


def test_actions_reject_non_immediate_activation_mode_without_trade_action() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:4].upper()}"

    for payload in (
        {
            "id": downstream_id,
            "description": "downstream target",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
        {
            "id": upstream_id,
            "description": "upstream source",
            "trade_type": "buy",
            "symbols": [{"code": "AAPL", "trade_type": "buy"}],
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    ):
        created = client.post("/v1/strategies", json=payload)
        assert created.status_code == 200

    updated = client.put(
        f"/v1/strategies/{upstream_id}/actions",
        json={
            "trade_action_json": None,
            "next_strategy_id": downstream_id,
            "next_strategy_note": "mode check",
            "next_strategy_activation_mode": "AFTER_TRADE_COMPLETED",
        },
    )
    assert updated.status_code == 422
    assert "next_strategy_activation_mode requires trade_action_json" in str(updated.json()["detail"])


def test_actions_persist_next_strategy_activation_mode_when_trade_action_exists() -> None:
    downstream_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:4].upper()}"

    for payload in (
        {
            "id": downstream_id,
            "description": "downstream target",
            "trade_type": "buy",
            "symbols": [{"code": "SLV", "trade_type": "buy"}],
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
        {
            "id": upstream_id,
            "description": "upstream source",
            "trade_type": "buy",
            "symbols": [{"code": "AAPL", "trade_type": "buy"}],
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "conditions": [],
        },
    ):
        created = client.post("/v1/strategies", json=payload)
        assert created.status_code == 200

    updated = client.put(
        f"/v1/strategies/{upstream_id}/actions",
        json={
            "trade_action_json": {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            },
            "next_strategy_id": downstream_id,
            "next_strategy_note": "mode persist",
            "next_strategy_activation_mode": "AFTER_TRADE_SUBMITTED",
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["next_strategy_activation_mode"] == "AFTER_TRADE_SUBMITTED"

    detail = client.get(f"/v1/strategies/{upstream_id}")
    assert detail.status_code == 200
    assert detail.json()["next_strategy_activation_mode"] == "AFTER_TRADE_SUBMITTED"


def test_delete_strategy_uses_soft_delete_and_hides_strategy() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
    downstream_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:4].upper()}"

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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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


def test_delete_verifying_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json={
            "id": strategy_id,
            "description": "verifying delete guard",
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
        conn.execute("UPDATE strategies SET status = 'VERIFYING' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_delete"] is False
    assert "VERIFYING" in (detail.json()["capability_reasons"]["can_delete"] or "")

    deleted = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted.status_code == 409
    assert "VERIFYING" in deleted.json()["detail"]


def test_delete_triggered_strategy_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
    created = client.post(
        "/v1/strategies",
        json={
            "id": strategy_id,
            "description": "triggered delete guard",
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
        conn.execute("UPDATE strategies SET status = 'TRIGGERED' WHERE id = ?", (strategy_id,))
        conn.commit()

    detail = client.get(f"/v1/strategies/{strategy_id}")
    assert detail.status_code == 200
    assert detail.json()["capabilities"]["can_delete"] is False
    assert "TRIGGERED" in (detail.json()["capability_reasons"]["can_delete"] or "")

    deleted = client.delete(f"/v1/strategies/{strategy_id}")
    assert deleted.status_code == 409
    assert "TRIGGERED" in deleted.json()["detail"]


def test_delete_strategy_with_active_trade_instruction_is_rejected() -> None:
    strategy_id = f"S-UT-{uuid4().hex[:4].upper()}"
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
    downstream_id = f"S-UT-{uuid4().hex[:4].upper()}"
    upstream_id = f"S-UT-{uuid4().hex[:4].upper()}"

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
