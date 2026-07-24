from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pandas as pd

try:
    from src.app_request_state import STATUS_LABELS, ensure_app_tables
    from src.work_time import LUNCH_WARNING, needs_lunch_warning
except ModuleNotFoundError:
    from app_request_state import STATUS_LABELS, ensure_app_tables
    from work_time import LUNCH_WARNING, needs_lunch_warning


def lunch_warning_comment(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return LUNCH_WARNING if needs_lunch_warning(str(value).strip()) else ""
    except ValueError:
        return ""


def build_report_dataframe(db_path: str, date_from: date | None = None, date_to: date | None = None) -> pd.DataFrame:
    planned_date_filter = ""
    params: list[str] = []

    if date_from and date_to:
        planned_date_filter = "AND r.planned_work_date BETWEEN ? AND ?"
        params.extend([date_from.isoformat(), date_to.isoformat()])

    query = f"""
    WITH planned_candidates AS (
        SELECT
            r.response_id,
            r.full_name_key,
            COALESCE(r.full_name_normalized, r.full_name) AS full_name,
            COALESCE(st.override_planned_work_date, r.planned_work_date) AS planned_work_date,
            COALESCE(st.override_planned_work_time, r.planned_work_time) AS planned_work_time,
            COALESCE(st.override_payment_type, r.payment_type) AS exit_conditions,
            COALESCE(st.override_task_description, r.task_description) AS task_description,
            COALESCE(st.status, 'active') AS request_status,
            st.actual_work_date AS web_actual_work_date,
            st.actual_work_time AS web_actual_work_time
        FROM survey_responses r
        LEFT JOIN app_request_state st ON st.response_id = r.response_id
        WHERE r.request_type = 'Подать заявку'
          AND COALESCE(st.override_planned_work_date, r.planned_work_date) IS NOT NULL
          {planned_date_filter.replace("r.planned_work_date", "COALESCE(st.override_planned_work_date, r.planned_work_date)")}
    ),
    planned_counts AS (
        SELECT
            full_name_key,
            planned_work_date,
            SUM(CASE WHEN request_status <> 'cancelled' THEN 1 ELSE 0 END) AS planned_count
        FROM planned_candidates
        GROUP BY full_name_key, planned_work_date
    ),
    legacy_actual_grouped AS (
        SELECT
            r.full_name_key,
            r.actual_work_date,
            COUNT(*) AS actual_count,
            MAX(r.actual_work_time) AS actual_work_time
        FROM survey_responses r
        WHERE r.request_type = 'Указать отработанное время'
          AND r.actual_work_date IS NOT NULL
          AND r.actual_work_time IS NOT NULL
        GROUP BY r.full_name_key, r.actual_work_date
    ),
    reconciled AS (
        SELECT
            p.*,
            CASE
                WHEN p.web_actual_work_date IS NOT NULL AND p.web_actual_work_time IS NOT NULL
                    THEN p.web_actual_work_date
                WHEN p.request_status <> 'cancelled' AND pc.planned_count = 1 AND la.actual_count = 1
                    THEN la.actual_work_date
                ELSE NULL
            END AS actual_work_date,
            CASE
                WHEN p.web_actual_work_date IS NOT NULL AND p.web_actual_work_time IS NOT NULL
                    THEN p.web_actual_work_time
                WHEN p.request_status <> 'cancelled' AND pc.planned_count = 1 AND la.actual_count = 1
                    THEN la.actual_work_time
                ELSE NULL
            END AS actual_work_time
        FROM planned_candidates p
        JOIN planned_counts pc
          ON pc.full_name_key = p.full_name_key
         AND pc.planned_work_date = p.planned_work_date
        LEFT JOIN legacy_actual_grouped la
          ON la.full_name_key = p.full_name_key
         AND la.actual_work_date = p.planned_work_date
    )
    SELECT
        r.full_name,
        r.planned_work_date,
        r.planned_work_time,
        r.exit_conditions,
        r.task_description,
        r.request_status,
        CASE
            WHEN r.request_status = 'cancelled' THEN 'Не требуется'
            WHEN r.actual_work_date IS NOT NULL AND r.actual_work_time IS NOT NULL THEN 'Да'
            ELSE 'Нет'
        END AS has_actual_time,
        r.actual_work_date,
        r.actual_work_time
    FROM reconciled r
    ORDER BY r.planned_work_date, r.full_name, r.response_id;
    """

    with closing(sqlite3.connect(db_path)) as conn, conn:
        ensure_app_tables(conn)
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(
            columns=[
                "ФИО",
                "Плановая дата выхода",
                "Плановое время работ",
                "Условия выхода",
                "Перечень задач",
                "Статус заявки",
                "Предоставил фактически отработанное время",
                "Дата фактического выхода",
                "Фактически отработанное время",
                "Комментарий к плановому времени",
                "Комментарий к фактическому времени",
            ]
        )

    df["planned_work_date"] = pd.to_datetime(df["planned_work_date"], errors="coerce").dt.strftime("%d.%m.%Y")
    df["actual_work_date"] = pd.to_datetime(df["actual_work_date"], errors="coerce").dt.strftime("%d.%m.%Y")
    df["request_status"] = df["request_status"].map(lambda value: STATUS_LABELS.get(value, value))
    df["planned_comment"] = df["planned_work_time"].map(lunch_warning_comment)
    df["actual_comment"] = df["actual_work_time"].map(lunch_warning_comment)

    report_df = df.rename(
        columns={
            "full_name": "ФИО",
            "planned_work_date": "Плановая дата выхода",
            "planned_work_time": "Плановое время работ",
            "exit_conditions": "Условия выхода",
            "task_description": "Перечень задач",
            "request_status": "Статус заявки",
            "has_actual_time": "Предоставил фактически отработанное время",
            "actual_work_date": "Дата фактического выхода",
            "actual_work_time": "Фактически отработанное время",
            "planned_comment": "Комментарий к плановому времени",
            "actual_comment": "Комментарий к фактическому времени",
        }
    )

    return report_df[
        [
            "ФИО",
            "Плановая дата выхода",
            "Плановое время работ",
            "Условия выхода",
            "Перечень задач",
            "Статус заявки",
            "Предоставил фактически отработанное время",
            "Дата фактического выхода",
            "Фактически отработанное время",
            "Комментарий к плановому времени",
            "Комментарий к фактическому времени",
        ]
    ]


def save_report(report_df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        report_df.to_excel(writer, sheet_name="Отчет 4", index=False)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Формирование отчета №4 по сверке заявок и фактического времени")
    parser.add_argument("--db", default="survey_results.db", help="Путь к SQLite БД")
    parser.add_argument("--date-from", help="Опционально: начало периода по плановой дате (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="Опционально: конец периода по плановой дате (YYYY-MM-DD)")
    parser.add_argument(
        "--output",
        help="Путь для xlsx-отчета. По умолчанию: reports/management_report_4_reconciliation.xlsx",
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

    default_output = Path("reports") / "management_report_4_reconciliation.xlsx"
    output_path = Path(args.output) if args.output else default_output

    report_df = build_report_dataframe(str(db_path), date_from, date_to)
    save_report(report_df, output_path)

    if date_from and date_to:
        print(f"Фильтр по плановой дате: {date_from.isoformat()} .. {date_to.isoformat()}")
    else:
        print("Фильтр по дате: не задан (все заявки)")
    print(f"Строк в отчете: {len(report_df)}")
    print(f"Файл: {output_path}")


if __name__ == "__main__":
    main()
