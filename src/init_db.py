from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

try:
    from src.app_request_state import ensure_app_tables
    from src.db_schema import ensure_core_tables
except ModuleNotFoundError:
    from app_request_state import ensure_app_tables
    from db_schema import ensure_core_tables


def initialize_database(db_path: str | Path) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        ensure_core_tables(conn)
        ensure_app_tables(conn)
        conn.commit()
    return path


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Инициализация Web-only БД Work on Holiday")
    parser.add_argument("--db", default="survey_results.db", help="Путь к SQLite БД")
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    path = initialize_database(args.db)
    print(f"БД инициализирована: {path}")


if __name__ == "__main__":
    main()
