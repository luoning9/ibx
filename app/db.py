from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .runtime_paths import resolve_data_dir

DEFAULT_DB_PATH = resolve_data_dir() / "ibx.sqlite3"
SCHEMA_PATH = Path(__file__).with_name("sql").joinpath("schema_v1.sql")


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env_path = os.getenv("IBX_DB_PATH")
    if env_path:
        return Path(env_path)
    return resolve_data_dir() / "ibx.sqlite3"


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _strategies_has_upstream_fk(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA foreign_key_list(strategies)").fetchall()
    return any(str(row["from"]) == "upstream_strategy_id" for row in rows)


def _strategies_has_broken_next_fk(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA foreign_key_list(strategies)").fetchall()
    return any(
        str(row["from"]) == "next_strategy_id" and str(row["table"]) == "strategies__new"
        for row in rows
    )


def _rebuild_strategies_without_upstream_fk(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS v_strategies_active")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS strategies__new")
    conn.execute(
        """
        CREATE TABLE strategies__new (
          id TEXT PRIMARY KEY,
          idempotency_key TEXT UNIQUE,
          description TEXT NOT NULL,
          trade_type TEXT NOT NULL
            CHECK (trade_type IN ("buy", "sell", "switch", "open", "close", "spread")),
          currency TEXT NOT NULL DEFAULT "USD"
            CHECK (currency = "USD"),
          upstream_only_activation INTEGER NOT NULL DEFAULT 0
            CHECK (upstream_only_activation IN (0, 1)),
          expire_mode TEXT NOT NULL
            CHECK (expire_mode IN ("relative", "absolute")),
          expire_in_seconds INTEGER
            CHECK (expire_in_seconds IS NULL OR (expire_in_seconds BETWEEN 1 AND 604800)),
          expire_at TEXT,
          status TEXT NOT NULL
            CHECK (status IN (
              "PENDING_ACTIVATION", "ACTIVE", "PAUSED", "TRIGGERED", "ORDER_SUBMITTED",
              "FILLED", "EXPIRED", "CANCELLED", "FAILED"
            )),
          condition_logic TEXT NOT NULL DEFAULT "AND"
            CHECK (condition_logic IN ("AND", "OR")),
          conditions_json TEXT NOT NULL DEFAULT "[]"
            CHECK (json_valid(conditions_json)),
          trade_action_json TEXT
            CHECK (trade_action_json IS NULL OR json_valid(trade_action_json)),
          next_strategy_id TEXT REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE SET NULL,
          next_strategy_note TEXT,
          upstream_strategy_id TEXT,
          is_deleted INTEGER NOT NULL DEFAULT 0
            CHECK (is_deleted IN (0, 1)),
          deleted_at TEXT,
          anchor_price REAL,
          activated_at TEXT,
          logical_activated_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1
            CHECK (version > 0),
          CHECK (next_strategy_id IS NULL OR next_strategy_id <> id),
          CHECK (upstream_strategy_id IS NULL OR upstream_strategy_id <> id),
          CHECK (
            (expire_mode = "relative" AND expire_in_seconds IS NOT NULL)
            OR
            (expire_mode = "absolute" AND expire_at IS NOT NULL)
          )
        )
        """
    )
    conn.execute(
        """
        INSERT INTO strategies__new (
          id, idempotency_key, description, trade_type, currency, upstream_only_activation,
          expire_mode, expire_in_seconds, expire_at, status, condition_logic, conditions_json,
          trade_action_json, next_strategy_id, next_strategy_note, upstream_strategy_id,
          is_deleted, deleted_at, anchor_price, activated_at, logical_activated_at,
          created_at, updated_at, version
        )
        SELECT
          id, idempotency_key, description, trade_type, currency, upstream_only_activation,
          expire_mode, expire_in_seconds, expire_at, status, condition_logic, conditions_json,
          trade_action_json, next_strategy_id, next_strategy_note, upstream_strategy_id,
          COALESCE(is_deleted, 0), deleted_at, anchor_price, activated_at, logical_activated_at,
          created_at, updated_at, version
        FROM strategies
        """
    )
    conn.execute("DROP TABLE strategies")
    conn.execute("ALTER TABLE strategies__new RENAME TO strategies")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    strategy_columns = _table_columns(conn, "strategies")
    if "upstream_strategy_id" not in strategy_columns:
        conn.execute(
            """
            ALTER TABLE strategies
            ADD COLUMN upstream_strategy_id TEXT
            """
        )
    if "is_deleted" not in strategy_columns:
        conn.execute(
            """
            ALTER TABLE strategies
            ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0
            """
        )
    if "deleted_at" not in strategy_columns:
        conn.execute(
            """
            ALTER TABLE strategies
            ADD COLUMN deleted_at TEXT
            """
        )

    if _strategies_has_upstream_fk(conn) or _strategies_has_broken_next_fk(conn):
        _rebuild_strategies_without_upstream_fk(conn)

    conn.execute("UPDATE strategies SET is_deleted = 0 WHERE is_deleted IS NULL")

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategies_upstream_strategy_id
        ON strategies (upstream_strategy_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategies_is_deleted_updated
        ON strategies (is_deleted, updated_at DESC)
        """
    )
    conn.execute("DROP VIEW IF EXISTS v_strategies_active")
    conn.execute(
        """
        CREATE VIEW v_strategies_active AS
        SELECT * FROM strategies WHERE is_deleted = 0
        """
    )

    # Backfill reverse link for historical rows that already had next_strategy_id.
    rows = conn.execute(
        """
        SELECT id, next_strategy_id
        FROM strategies
        WHERE next_strategy_id IS NOT NULL AND is_deleted = 0
        ORDER BY updated_at ASC, id ASC
        """
    ).fetchall()
    for row in rows:
        downstream_id = row["next_strategy_id"]
        hit = conn.execute(
            "SELECT upstream_strategy_id FROM strategies WHERE id = ?",
            (downstream_id,),
        ).fetchone()
        if hit is None:
            continue
        if hit["upstream_strategy_id"] in (None, ""):
            conn.execute(
                "UPDATE strategies SET upstream_strategy_id = ? WHERE id = ?",
                (row["id"], downstream_id),
            )


def init_db(db_path: str | Path | None = None) -> Path:
    path = resolve_db_path(db_path)
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"schema file not found: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(path) as conn:
        conn.executescript(schema_sql)
        _migrate_schema(conn)
        conn.commit()
    return path
