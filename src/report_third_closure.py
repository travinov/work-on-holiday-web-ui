from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd


def build_report_dataframe(db_path: str, date_from: date | None = None, date_to: date | None = None) -> pd.DataFrame:
    date_filter = ""
    params: list[str] = []

    if date_from and date_to:
        date_filter = "AND r.actual_work_date BETWEEN ? AND ?"
        params.extend([date_from.isoformat(), date_to.isoformat()])

    query = f"""
    WITH candidates AS (
        SELECT
            r.*,
            ROW_NUMBER() OVER (
                PARTITION BY r.full_name_key, r.actual_work_date
                ORDER BY
                    COALESCE(r.start_time, '') DESC,
                    COALESCE(r.source_row, 0) DESC,
                    r.response_id DESC
            ) AS rn
        FROM survey_responses r
        WHERE r.request_type = 'Указать отработанное время'
          AND r.actual_work_date IS NOT NULL
          AND r.actual_work_time IS NOT NULL
          {date_filter}
    )
    SELECT
        COALESCE(c.full_name_normalized, c.full_name) AS full_name,
        c.actual_work_date,
        c.actual_work_time
    FROM candidates c
    WHERE c.rn = 1
    ORDER BY c.actual_work_date, full_name;
    """

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(
            columns=[
                "ФИО",
                "Дата фактического выхода",
                "Фактически отработанное время",
            ]
        )

    df["actual_work_date"] = pd.to_datetime(df["actual_work_date"], errors="coerce").dt.strftime("%d.%m.%Y")

    report_df = df.rename(
        columns={
            "full_name": "ФИО",
            "actual_work_date": "Дата фактического выхода",
            "actual_work_time": "Фактически отработанное время",
        }
    )

    return report_df[["ФИО", "Дата фактического выхода", "Фактически отработанное время"]]


def save_report(report_df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        report_df.to_excel(writer, sheet_name="Отчет 3", index=False)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Формирование отчета №3 для закрытия заявок в Пульсе")
    parser.add_argument("--db", default="survey_results.db", help="Путь к SQLite БД")
    parser.add_argument("--date-from", help="Опционально: начало периода по фактической дате (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="Опционально: конец периода по фактической дате (YYYY-MM-DD)")
    parser.add_argument(
        "--output",
        help="Путь для xlsx-отчета. По умолчанию: reports/management_report_3_actual.xlsx",
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"БД не найдена: {db_path}")

    date_from = parse_date(args.date_from) if args.date_from else None
    date_to = parse_date(args.date_to) if args.date_to else None

    if (date_from is None) != (date_to is None):
        raise ValueError("Нужно указать обе даты: --date-from и --date-to")
    if date_from and date_to and date_from > date_to:
        raise ValueError("date-from не может быть позже date-to")

    default_output = Path("reports") / "management_report_3_actual.xlsx"
    output_path = Path(args.output) if args.output else default_output

    report_df = build_report_dataframe(str(db_path), date_from, date_to)
    save_report(report_df, output_path)

    if date_from and date_to:
        print(f"Фильтр по фактической дате: {date_from.isoformat()} .. {date_to.isoformat()}")
    else:
        print("Фильтр по дате: не задан (все фактические выходы)")
    print(f"Строк в отчете: {len(report_df)}")
    print(f"Файл: {output_path}")


if __name__ == "__main__":
    main()
