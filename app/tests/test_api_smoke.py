from fastapi.testclient import TestClient

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
    assert len(data) >= 1


def test_strategy_detail_contains_trade_type_and_symbols() -> None:
    resp = client.get("/v1/strategies/S0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_type"] in {"buy", "sell", "switch", "open", "close", "spread"}
    assert isinstance(body["symbols"], list)
    assert len(body["symbols"]) >= 1
    assert "code" in body["symbols"][0]
    assert "trade_type" in body["symbols"][0]


def test_create_strategy_uses_symbols_schema() -> None:
    payload = {
        "id": "S-UT-001",
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
    assert body["id"] == "S-UT-001"
    assert body["trade_type"] == "buy"
    assert body["symbols"][0]["code"] == "SLV"


def test_create_futures_open_strategy_with_open_symbol_leg() -> None:
    payload = {
        "id": "S-UT-002",
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
    payload = {
        "id": "S-UT-003",
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
