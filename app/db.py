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


def init_db(db_path: str | Path | None = None) -> Path:
    path = resolve_db_path(db_path)
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"schema file not found: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(path) as conn:
        conn.executescript(schema_sql)
        conn.commit()
    return path
