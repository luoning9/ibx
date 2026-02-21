from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import get_connection, init_db


def test_init_db_creates_core_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ibx_test.sqlite3"
    init_db(db_path=db_path)

    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows}

    expected = {
        "strategies",
        "strategy_symbols",
        "strategy_events",
        "orders",
        "verification_events",
        "trade_logs",
        "trade_instructions",
    }
    assert expected.issubset(names)


def test_strategies_json_columns_are_validated(tmp_path: Path) -> None:
    db_path = tmp_path / "ibx_test.sqlite3"
    init_db(db_path=db_path)

    with get_connection(db_path) as conn:
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO strategies (
                        id, description, trade_type, currency, upstream_only_activation,
                        expire_mode, expire_in_seconds, status, condition_logic, conditions_json,
                        trade_action_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "S-DB-002",
                        "json check test",
                        "buy",
                        "USD",
                        0,
                        "relative",
                        3600,
                        "PENDING_ACTIVATION",
                        "AND",
                        "not-json",
                        None,
                        "2026-02-21T00:00:00Z",
                        "2026-02-21T00:00:00Z",
                    ),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            return

    raise AssertionError("expected sqlite IntegrityError for invalid conditions_json")


def test_active_view_excludes_soft_deleted_strategies(tmp_path: Path) -> None:
    db_path = tmp_path / "ibx_test.sqlite3"
    init_db(db_path=db_path)

    with get_connection(db_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO strategies (
                    id, description, trade_type, currency, upstream_only_activation,
                    expire_mode, expire_in_seconds, status, condition_logic, conditions_json,
                    trade_action_json, next_strategy_id, next_strategy_note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "S-UPSTREAM",
                    "upstream",
                    "buy",
                    "USD",
                    0,
                    "relative",
                    3600,
                    "PENDING_ACTIVATION",
                    "AND",
                    "[]",
                    None,
                    None,
                    None,
                    "2026-02-21T00:00:00Z",
                    "2026-02-21T00:00:00Z",
                ),
            )
            conn.execute(
                """
                UPDATE strategies
                SET is_deleted = 1, deleted_at = ?, status = 'CANCELLED'
                WHERE id = ?
                """,
                ("2026-02-22T00:00:00Z", "S-UPSTREAM"),
            )

            total_rows = conn.execute("SELECT COUNT(1) AS c FROM strategies").fetchone()
            active_rows = conn.execute("SELECT COUNT(1) AS c FROM v_strategies_active").fetchone()
            assert total_rows is not None
            assert active_rows is not None
            assert total_rows["c"] == 1
            assert active_rows["c"] == 0
