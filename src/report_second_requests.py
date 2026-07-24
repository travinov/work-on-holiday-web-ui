from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pandas as pd

try:
    from src.app_request_state import ensure_app_tables
    from src.work_time import LUNCH_WARNING, needs_lunch_warning
except ModuleNotFoundError:
    from app_request_state import ensure_app_tables
    from work_time import LUNCH_WARNING, needs_lunch_warning


def lunch_warning_comment(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return LUNCH_WARNING if needs_lunch_warning(str(value).strip()) else ""
    except ValueError:
        return ""


def build_report_dataframe(db_path: str, date_from: date | None = None, date_to: date | None = None) -> pd.DataFrame:
    candidate_date_filter = ""
    params: list[str] = []

    if date_from and date_to:
        candidate_date_filter = "AND r.planned_work_date BETWEEN ? AND ?"
        params.extend([date_from.isoformat(), date_to.isoformat()])

    query = f"""
    WITH candidates AS (
        SELECT
            r.response_id,
            r.full_name_key,
            COALESCE(r.full_name_normalized, r.full_name) AS full_name,
            COALESCE(st.override_planned_work_date, r.planned_work_date) AS planned_work_date,
            COALESCE(st.override_planned_work_time, r.planned_work_time) AS planned_work_time,
            COALESCE(st.override_payment_type, r.payment_type) AS exit_conditions,
            COALESCE(st.override_task_description, r.task_description) AS task_description,
            COALESCE(st.override_justification, r.justification) AS justification,
            COALESCE(st.status, 'active') AS request_status
        FROM survey_responses r
        LEFT JOIN app_request_state st ON st.response_id = r.response_id
        WHERE r.request_type = 'Подать заявку'
          AND COALESCE(st.override_planned_work_date, r.planned_work_date) IS NOT NULL
          {candidate_date_filter.replace("r.planned_work_date", "COALESCE(st.override_planned_work_date, r.planned_work_date)")}
    ),
    actual_dates AS (
        SELECT
            r.full_name_key,
            r.actual_work_date
        FROM survey_responses r
        WHERE r.request_type = 'Указать отработанное время'
          AND r.actual_work_date IS NOT NULL
          AND r.actual_work_time IS NOT NULL
        UNION
        SELECT
            r.full_name_key,
            st.actual_work_date
        FROM app_request_state st
        JOIN survey_responses r ON r.response_id = st.response_id
        WHERE st.actual_work_date IS NOT NULL
          AND st.actual_work_time IS NOT NULL
    )
    SELECT
        c.full_name,
        (
            SELECT COUNT(*)
            FROM actual_dates ac2
            WHERE ac2.full_name_key = c.full_name_key
              AND ac2.actual_work_date BETWEEN date(c.planned_work_date, '-29 day') AND c.planned_work_date
        ) AS exits_last_month,
        c.exit_conditions,
        c.task_description,
        c.justification,
        c.planned_work_date,
        c.planned_work_time
    FROM candidates c
    WHERE c.request_status IN ('active', 'in_progress', 'in_fact', 'completed')
    ORDER BY c.planned_work_date, c.full_name;
    """

    with closing(sqlite3.connect(db_path)) as conn, conn:
        ensure_app_tables(conn)
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(
            columns=[
                "ФИО",
                "Количество выходов за последний месяц",
                "Условия выхода",
                "Перечень задач",
                "Обоснование привлечения",
                "Плановая дата выхода",
                "Плановое время работ",
                "Комментарий",
            ]
        )

    df["planned_work_date"] = pd.to_datetime(df["planned_work_date"], errors="coerce").dt.strftime("%d.%m.%Y")
    df["exits_last_month"] = pd.to_numeric(df["exits_last_month"], errors="coerce").fillna(0).astype(int)
    df["comment"] = df["planned_work_time"].map(lunch_warning_comment)

    report_df = df.rename(
        columns={
            "full_name": "ФИО",
            "exits_last_month": "Количество выходов за последний месяц",
            "exit_conditions": "Условия выхода",
            "task_description": "Перечень задач",
            "justification": "Обоснование привлечения",
            "planned_work_date": "Плановая дата выхода",
            "planned_work_time": "Плановое время работ",
            "comment": "Комментарий",
        }
    )

    return report_df[
        [
            "ФИО",
            "Количество выходов за последний месяц",
            "Условия выхода",
            "Перечень задач",
            "Обоснование привлечения",
            "Плановая дата выхода",
            "Плановое время работ",
            "Комментарий",
        ]
    ]


def save_report(report_df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        report_df.to_excel(writer, sheet_name="Отчет 2", index=False)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Формирование отчета №2 для заведения заявок")
    parser.add_argument("--db", default="survey_results.db", help="Путь к SQLite БД")
    parser.add_argument("--date-from", help="Опционально: начало периода по плановой дате (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="Опционально: конец периода по плановой дате (YYYY-MM-DD)")
    parser.add_argument(
        "--output",
        help="Путь для xlsx-отчета. По умолчанию: reports/management_report_2_planned.xlsx",
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

    default_output = Path("reports") / "management_report_2_planned.xlsx"
    output_path = Path(args.output) if args.output else default_output

    report_df = build_report_dataframe(str(db_path), date_from, date_to)
    save_report(report_df, output_path)

    if date_from and date_to:
        print(f"Фильтр по плановой дате: {date_from.isoformat()} .. {date_to.isoformat()}")
    else:
        print("Фильтр по дате: не задан (все неотмененные заявки)")
    print(f"Строк в отчете: {len(report_df)}")
    print(f"Файл: {output_path}")


if __name__ == "__main__":
    main()
