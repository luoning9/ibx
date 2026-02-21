from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize IBX SQLite schema")
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite file path (defaults to IBX_DB_PATH or data/ibx.sqlite3)",
    )
    args = parser.parse_args()

    db_path = init_db(db_path=args.db_path)
    print(f"[OK] Initialized database schema at: {db_path}")


if __name__ == "__main__":
    main()
