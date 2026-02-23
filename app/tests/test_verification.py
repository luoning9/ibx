from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.db import get_connection, init_db
from app.verification import run_activation_verification


UTC = timezone.utc


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _insert_strategy(
    strategy_id: str,
    *,
    db_path,
    market: str = "US_STOCK",
    trade_type: str = "buy",
    conditions_json: str = "[]",
) -> None:
    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, description, market, sec_type, exchange, trade_type, currency,
                upstream_only_activation, expire_mode, expire_in_seconds, expire_at,
                status, condition_logic, conditions_json, trade_action_json,
                created_at, updated_at, activated_at, logical_activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                f"verify {strategy_id}",
                market,
                "STK",
                "SMART",
                trade_type,
                "USD",
                0,
                "relative",
                86400,
                None,
                "VERIFYING",
                "AND",
                conditions_json,
                None,
                now_iso,
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()


def _insert_symbol(strategy_id: str, *, db_path, position: int, code: str, contract_id: int | None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_symbols (
                strategy_id, position, code, trade_type, contract_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (strategy_id, position, code, "buy", contract_id, _iso_now()),
        )
        conn.commit()


class _FakeBrokerProvider:
    def __init__(self, *, contract_ids: dict[str, int], fail_snapshot: bool = False) -> None:
        self._contract_ids = {k.upper(): v for k, v in contract_ids.items()}
        self._fail_snapshot = fail_snapshot

    def get_account_snapshot(self, *, account_code: str | None = None):  # type: ignore[no-untyped-def]
        if self._fail_snapshot:
            raise RuntimeError("snapshot unavailable")
        return SimpleNamespace(account_code=account_code, positions=[], values={}, values_float={})

    def resolve_contract_id(  # type: ignore[no-untyped-def]
        self,
        *,
        code: str,
        market: str,
        contract_month: str | None = None,
    ) -> int:
        _ = (market, contract_month)
        key = code.strip().upper()
        if key not in self._contract_ids:
            raise RuntimeError(f"unknown symbol: {key}")
        return self._contract_ids[key]


def test_run_activation_verification_resolves_symbol_and_condition_contract_ids(tmp_path) -> None:
    db_path = tmp_path / "ibx_verify_success.sqlite3"
    init_db(db_path=db_path)
    strategy_id = "S-VERIFY-OK"
    _insert_strategy(
        strategy_id,
        db_path=db_path,
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                },
                {
                    "condition_id": "c2",
                    "condition_type": "PAIR_PRODUCTS",
                    "metric": "SPREAD",
                    "trigger_mode": "LEVEL_CONFIRM",
                    "evaluation_window": "5m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 1.0,
                    "product": "AAPL",
                    "product_b": "MSFT",
                },
            ]
        ),
    )
    _insert_symbol(strategy_id, db_path=db_path, position=1, code="AAPL", contract_id=None)
    _insert_symbol(strategy_id, db_path=db_path, position=2, code="MSFT", contract_id=None)

    provider = _FakeBrokerProvider(contract_ids={"AAPL": 101, "MSFT": 202})
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM v_strategies_active WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        assert row is not None
        result = run_activation_verification(
            conn,
            strategy_id=strategy_id,
            strategy_row=row,
            broker_data_provider=provider,
        )
        conn.commit()

    assert result.passed is True
    assert result.resolved_symbol_contracts == 2
    assert result.updated_condition_contracts == 3

    with get_connection(db_path) as conn:
        symbol_rows = conn.execute(
            """
            SELECT code, contract_id
            FROM strategy_symbols
            WHERE strategy_id = ?
            ORDER BY position ASC
            """,
            (strategy_id,),
        ).fetchall()
        assert [row["contract_id"] for row in symbol_rows] == [101, 202]

        strategy_row = conn.execute(
            "SELECT conditions_json FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        assert strategy_row is not None
        conditions = json.loads(strategy_row["conditions_json"])
        assert conditions[0]["contract_id"] == 101
        assert conditions[1]["contract_id"] == 101
        assert conditions[1]["contract_id_b"] == 202


def test_run_activation_verification_fails_when_snapshot_unavailable(tmp_path) -> None:
    db_path = tmp_path / "ibx_verify_snapshot_fail.sqlite3"
    init_db(db_path=db_path)
    strategy_id = "S-VERIFY-SNAPSHOT-FAIL"
    _insert_strategy(
        strategy_id,
        db_path=db_path,
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                }
            ]
        ),
    )
    _insert_symbol(strategy_id, db_path=db_path, position=1, code="AAPL", contract_id=None)

    provider = _FakeBrokerProvider(contract_ids={"AAPL": 101}, fail_snapshot=True)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM v_strategies_active WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        assert row is not None
        result = run_activation_verification(
            conn,
            strategy_id=strategy_id,
            strategy_row=row,
            broker_data_provider=provider,
        )

    assert result.passed is False
    assert "get_account_snapshot failed" in result.reason
