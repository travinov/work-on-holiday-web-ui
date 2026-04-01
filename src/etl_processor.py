from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

SHEET_NAME = "Страница 1"
EXPECTED_COLUMN_COUNT = 31
SYSTEM_COLUMNS = [
    "system_1",
    "system_2",
    "system_3",
    "system_4",
    "system_5",
    "system_6",
]
TRUTHY_VALUES = {"да", "true", "1", "yes", "y", "согласен"}
FALSY_VALUES = {"нет", "false", "0", "no", "n", "не согласен"}

COLUMN_BY_INDEX = {
    0: "source_id",
    1: "parameter",
    2: "start_time",
    3: "duration_raw",
    4: "channel",
    5: "status",
    6: "comment",
    7: "agreement_raw",
    8: "full_name",
    9: "request_type",
    10: "grade_12_plus_raw",
    11: "payment_option_4",
    12: "payment_option_5",
    13: "task_description",
    14: "justification",
    15: "system_1",
    16: "need_additional_system_1_raw",
    17: "system_2",
    18: "need_additional_system_2_raw",
    19: "system_3",
    20: "need_additional_system_3_raw",
    21: "system_4",
    22: "need_additional_system_4_raw",
    23: "system_5",
    24: "need_additional_system_5_raw",
    25: "system_6",
    26: "planned_work_date",
    27: "planned_work_time",
    28: "approver",
    29: "actual_work_date",
    30: "actual_work_time",
}

RESPONSES_COLUMNS = [
    "response_id",
    "source_row",
    "source_id",
    "start_time",
    "duration_raw",
    "duration_seconds",
    "duration_minutes",
    "channel",
    "status",
    "comment",
    "agreement",
    "full_name",
    "full_name_normalized",
    "full_name_key",
    "request_type",
    "grade_12_plus",
    "payment_type",
    "task_description",
    "justification",
    "planned_work_date",
    "planned_work_time",
    "approver",
    "actual_work_date",
    "actual_work_time",
    "target_work_date",
    "logical_key",
    "need_additional_system_1",
    "need_additional_system_2",
    "need_additional_system_3",
    "need_additional_system_4",
    "need_additional_system_5",
    "system_1",
    "system_2",
    "system_3",
    "system_4",
    "system_5",
    "system_6",
    "row_hash",
    "source_file",
    "source_file_hash",
    "loaded_at",
]

RESPONSES_BASE_COLUMNS = [column for column in RESPONSES_COLUMNS if column != "response_id"]

ROW_HASH_COLUMNS = [
    "source_id",
    "start_time",
    "duration_raw",
    "channel",
    "status",
    "agreement",
    "full_name_key",
    "request_type",
    "grade_12_plus",
    "payment_type",
    "task_description",
    "justification",
    "planned_work_date",
    "planned_work_time",
    "actual_work_date",
    "actual_work_time",
    "approver",
    "need_additional_system_1",
    "need_additional_system_2",
    "need_additional_system_3",
    "need_additional_system_4",
    "need_additional_system_5",
    "system_1",
    "system_2",
    "system_3",
    "system_4",
    "system_5",
    "system_6",
]

TIME_RANGE_PATTERN = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})\s*$")


def clean_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.lower() in {"nan", "none", "null"}:
        return None
    return text_value


def normalize_name(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    collapsed = " ".join(cleaned.split())
    return " ".join(part.capitalize() for part in collapsed.split(" "))


def normalize_name_key(value: Any) -> str | None:
    normalized = normalize_name(value)
    if normalized is None:
        return None
    return normalized.lower().replace("ё", "е")


def parse_bool(value: Any) -> int | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    lowered = cleaned.lower()
    if lowered in TRUTHY_VALUES:
        return 1
    if lowered in FALSY_VALUES:
        return 0
    return None


def parse_datetime_to_iso(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.to_pydatetime().replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")

    parsed = pd.to_datetime(cleaned, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(microsecond=0).isoformat(sep=" ")


def parse_date_to_iso(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    parsed = pd.to_datetime(cleaned, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def parse_duration_seconds(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        value = value.to_pydatetime().time()
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second

    cleaned = clean_text(value)
    if cleaned is None:
        return None

    parsed = pd.to_timedelta(cleaned, errors="coerce")
    if pd.isna(parsed):
        return None
    return int(parsed.total_seconds())


def parse_time_range_to_minutes(value: Any) -> tuple[int, int] | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None

    match = TIME_RANGE_PATTERN.match(cleaned)
    if not match:
        return None

    start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
    if start_hour > 23 or end_hour > 23 or start_minute > 59 or end_minute > 59:
        return None

    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def format_minutes_as_time(total_minutes: int) -> str:
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def normalize_cross_midnight_actual(
    actual_date_value: Any,
    actual_time_value: Any,
) -> tuple[str | None, str | None]:
    actual_date = clean_text(actual_date_value)
    actual_time = clean_text(actual_time_value)
    if actual_date is None or actual_time is None:
        return actual_date, actual_time

    time_range = parse_time_range_to_minutes(actual_time)
    if time_range is None:
        return actual_date, actual_time

    start_minutes, end_minutes = time_range
    if start_minutes <= end_minutes:
        return actual_date, actual_time

    try:
        parsed_date = datetime.strptime(actual_date, "%Y-%m-%d").date()
    except ValueError:
        fallback = pd.to_datetime(actual_date, errors="coerce", dayfirst=True)
        if pd.isna(fallback):
            return actual_date, actual_time
        parsed_date = fallback.date()

    duration_minutes = (24 * 60 - start_minutes) + end_minutes
    shifted_date = (parsed_date + timedelta(days=1)).isoformat()
    shifted_time = f"00:00 - {format_minutes_as_time(duration_minutes)}"

    return shifted_date, shifted_time


def normalize_request_type(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None

    lowered = cleaned.lower()
    if "подать" in lowered and "заяв" in lowered:
        return "Подать заявку"
    if "указать" in lowered and "отработ" in lowered:
        return "Указать отработанное время"
    return cleaned


def combine_payment_type(row: pd.Series) -> str | None:
    values = []
    for column in ("payment_option_4", "payment_option_5"):
        cleaned = clean_text(row.get(column))
        if cleaned and cleaned not in values:
            values.append(cleaned)
    if not values:
        return None
    return "; ".join(values)


def derive_target_work_date(row: pd.Series) -> str | None:
    request_type = row.get("request_type")
    planned_date = row.get("planned_work_date")
    actual_date = row.get("actual_work_date")

    if request_type == "Подать заявку":
        return planned_date
    if request_type == "Указать отработанное время":
        return actual_date or planned_date
    return planned_date or actual_date


def find_header_row(raw_df: pd.DataFrame) -> int:
    for index in range(min(20, len(raw_df))):
        row = [clean_text(value) for value in raw_df.iloc[index].tolist()]
        if "id" in row and "Параметр" in row and "Время начала" in row:
            return index
    raise ValueError("Не найдена строка заголовков (ожидаются id/Параметр/Время начала)")


def to_stable_string(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


def build_row_hash(row: pd.Series) -> str:
    payload = [to_stable_string(row.get(column)) for column in ROW_HASH_COLUMNS]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def list_input_files(input_dir: Path, recursive: bool = False) -> list[Path]:
    patterns = ("*.xlsx", "*.xlsm", "*.xls")
    files: list[Path] = []
    for pattern in patterns:
        found = input_dir.rglob(pattern) if recursive else input_dir.glob(pattern)
        files.extend(path for path in found if path.is_file() and not path.name.startswith("~$"))

    # Убираем дубли и сортируем по времени изменения.
    uniq_files = sorted(set(files), key=lambda path: (path.stat().st_mtime, path.name.lower()))
    return uniq_files


def hash_file(file_path: Path) -> str:
    sha = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        while True:
            chunk = file_handle.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
        (table_name,),
    )
    return cursor.fetchone() is not None


def ensure_ingestion_files_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingestion_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_hash TEXT NOT NULL UNIQUE,
            file_mtime TEXT,
            rows_in_file INTEGER NOT NULL,
            rows_inserted INTEGER NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )


def get_processed_hashes(conn: sqlite3.Connection) -> set[str]:
    ensure_ingestion_files_table(conn)
    rows = conn.execute("SELECT file_hash FROM ingestion_files").fetchall()
    return {row[0] for row in rows}


def ensure_responses_schema(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    for column in RESPONSES_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = None

    normalized = normalized[RESPONSES_COLUMNS]

    if "response_id" in normalized.columns:
        normalized["response_id"] = pd.to_numeric(normalized["response_id"], errors="coerce")

    return normalized


def build_responses_dataframe(input_file: str) -> pd.DataFrame:
    raw_df = pd.read_excel(input_file, sheet_name=SHEET_NAME, header=None, dtype=object)
    header_row = find_header_row(raw_df)

    data = raw_df.iloc[header_row + 1 :, :EXPECTED_COLUMN_COUNT].copy()
    data = data.dropna(how="all")
    data.columns = [COLUMN_BY_INDEX.get(i, f"col_{i}") for i in range(len(data.columns))]
    data["source_row"] = data.index + 1

    for text_column in [
        "comment",
        "full_name",
        "request_type",
        "payment_option_4",
        "payment_option_5",
        "task_description",
        "justification",
        "approver",
        "planned_work_time",
        "actual_work_time",
        *SYSTEM_COLUMNS,
    ]:
        data[text_column] = data[text_column].map(clean_text)

    data["source_id"] = data["source_id"].map(clean_text)
    data["source_id"] = data["source_id"].map(lambda value: int(value) if value and str(value).isdigit() else None)
    data["start_time"] = data["start_time"].map(parse_datetime_to_iso)
    data["planned_work_date"] = data["planned_work_date"].map(parse_date_to_iso)
    data["actual_work_date"] = data["actual_work_date"].map(parse_date_to_iso)
    data[["actual_work_date", "actual_work_time"]] = data.apply(
        lambda row: normalize_cross_midnight_actual(
            row.get("actual_work_date"),
            row.get("actual_work_time"),
        ),
        axis=1,
        result_type="expand",
    )
    data["duration_seconds"] = data["duration_raw"].map(parse_duration_seconds)
    data["duration_minutes"] = data["duration_seconds"].map(
        lambda value: round(value / 60.0, 2) if value is not None else None
    )
    data["agreement"] = data["agreement_raw"].map(parse_bool)
    data["grade_12_plus"] = data["grade_12_plus_raw"].map(parse_bool)
    data["request_type"] = data["request_type"].map(normalize_request_type)
    data["full_name_normalized"] = data["full_name"].map(normalize_name)
    data["full_name_key"] = data["full_name"].map(normalize_name_key)
    data["payment_type"] = data.apply(combine_payment_type, axis=1)
    data["target_work_date"] = data.apply(derive_target_work_date, axis=1)

    for column in [
        "need_additional_system_1_raw",
        "need_additional_system_2_raw",
        "need_additional_system_3_raw",
        "need_additional_system_4_raw",
        "need_additional_system_5_raw",
    ]:
        bool_column = column.replace("_raw", "")
        data[bool_column] = data[column].map(parse_bool)

    mask_actual_time = data["request_type"] == "Указать отработанное время"
    data.loc[mask_actual_time, ["task_description", "justification"]] = None

    meaningful_columns = ["full_name", "request_type", "planned_work_date", "actual_work_date", "source_id"]
    data = data[data[meaningful_columns].notna().any(axis=1)].copy()

    data["logical_key"] = data.apply(
        lambda row: (
            f"{row['full_name_key']}|{row['target_work_date']}|{(row['request_type'] or '').lower()}"
            if row["full_name_key"] and row["target_work_date"] and row["request_type"]
            else None
        ),
        axis=1,
    )

    data["row_hash"] = data.apply(build_row_hash, axis=1)
    data["source_file"] = None
    data["source_file_hash"] = None
    data["loaded_at"] = None

    return ensure_responses_schema(data)


def build_systems_dataframe(responses_df: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in responses_df.itertuples(index=False):
        for order, column in enumerate(SYSTEM_COLUMNS, start=1):
            system_name = clean_text(getattr(row, column))
            if system_name:
                records.append(
                    {
                        "response_id": int(row.response_id),
                        "system_order": order,
                        "system_name": system_name,
                    }
                )

    return pd.DataFrame(records, columns=["response_id", "system_order", "system_name"])


def ensure_supporting_objects(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_responses_row_hash
            ON survey_responses (row_hash);

        CREATE INDEX IF NOT EXISTS idx_responses_name_date_type
            ON survey_responses (full_name_key, target_work_date, request_type);

        CREATE INDEX IF NOT EXISTS idx_responses_request_type
            ON survey_responses (request_type);

        CREATE INDEX IF NOT EXISTS idx_systems_response_id
            ON response_systems (response_id);

        DROP VIEW IF EXISTS vw_response_summary;
        CREATE VIEW vw_response_summary AS
        SELECT
            r.response_id,
            r.full_name_normalized AS full_name,
            r.request_type,
            r.target_work_date,
            r.planned_work_time,
            r.actual_work_time,
            r.payment_type,
            r.approver,
            GROUP_CONCAT(s.system_name, ' | ') AS systems
        FROM survey_responses r
        LEFT JOIN response_systems s ON s.response_id = r.response_id
        GROUP BY
            r.response_id,
            r.full_name_normalized,
            r.request_type,
            r.target_work_date,
            r.planned_work_time,
            r.actual_work_time,
            r.payment_type,
            r.approver;
        """
    )


def read_existing_responses(conn: sqlite3.Connection) -> pd.DataFrame:
    if not table_exists(conn, "survey_responses"):
        return ensure_responses_schema(pd.DataFrame(columns=RESPONSES_COLUMNS))

    existing_df = pd.read_sql_query("SELECT * FROM survey_responses", conn)
    existing_df = ensure_responses_schema(existing_df)

    # Для исторических строк, где хеш не был рассчитан.
    missing_hash_mask = existing_df["row_hash"].isna() | (existing_df["row_hash"].astype(str).str.strip() == "")
    if missing_hash_mask.any():
        existing_df.loc[missing_hash_mask, "row_hash"] = existing_df[missing_hash_mask].apply(build_row_hash, axis=1)

    if not existing_df.empty:
        existing_df = existing_df.sort_values("response_id").drop_duplicates(subset=["row_hash"], keep="last")

    return existing_df


def save_dataset(
    conn: sqlite3.Connection,
    combined_df: pd.DataFrame,
    source_file: Path,
    file_hash: str,
    rows_in_file: int,
    rows_inserted: int,
) -> None:
    combined_df = ensure_responses_schema(combined_df)

    systems_df = build_systems_dataframe(combined_df)

    combined_df.to_sql("survey_responses", conn, if_exists="replace", index=False)
    systems_df.to_sql("response_systems", conn, if_exists="replace", index=False)

    ensure_ingestion_files_table(conn)
    ensure_supporting_objects(conn)

    file_mtime = datetime.fromtimestamp(source_file.stat().st_mtime).isoformat(timespec="seconds")
    processed_at = datetime.now().isoformat(timespec="seconds")

    conn.execute(
        """
        INSERT OR IGNORE INTO ingestion_files (
            file_path,
            file_name,
            file_hash,
            file_mtime,
            rows_in_file,
            rows_inserted,
            processed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(source_file),
            source_file.name,
            file_hash,
            file_mtime,
            rows_in_file,
            rows_inserted,
            processed_at,
        ),
    )


def append_delta_from_file(input_file: Path, db_path: str) -> tuple[int, int]:
    file_hash = hash_file(input_file)
    file_mtime = datetime.fromtimestamp(input_file.stat().st_mtime).isoformat(timespec="seconds")
    loaded_at = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as conn:
        processed_hashes = get_processed_hashes(conn)
        if file_hash in processed_hashes:
            return 0, -1

        new_df = build_responses_dataframe(str(input_file))
        new_df["source_file"] = str(input_file)
        new_df["source_file_hash"] = file_hash
        new_df["loaded_at"] = loaded_at

        existing_df = read_existing_responses(conn)
        existing_hashes = set(existing_df["row_hash"].dropna().astype(str).tolist())

        delta_df = new_df[~new_df["row_hash"].isin(existing_hashes)].copy()
        rows_inserted = len(delta_df)

        if rows_inserted > 0:
            if existing_df.empty:
                max_id = 0
            else:
                max_id = int(pd.to_numeric(existing_df["response_id"], errors="coerce").fillna(0).max())

            delta_df["response_id"] = range(max_id + 1, max_id + rows_inserted + 1)
            combined_df = pd.concat([existing_df, delta_df], ignore_index=True)
        else:
            combined_df = existing_df

        combined_df = ensure_responses_schema(combined_df)
        save_dataset(
            conn=conn,
            combined_df=combined_df,
            source_file=input_file,
            file_hash=file_hash,
            rows_in_file=len(new_df),
            rows_inserted=rows_inserted,
        )
        conn.commit()

    print(f"Обработан файл: {input_file.name}")
    print(f"Изменен: {file_mtime}")
    print(f"Строк в выгрузке: {len(new_df)}")
    print(f"Новых записей (дельта): {rows_inserted}")

    return rows_inserted, len(new_df)


def process_survey_data(input_file: str, db_path: str = "survey_results.db") -> pd.DataFrame:
    input_path = Path(input_file)
    if not input_path.exists():
        raise FileNotFoundError(f"Файл не найден: {input_path}")

    append_delta_from_file(input_path, db_path)

    with sqlite3.connect(db_path) as conn:
        if table_exists(conn, "survey_responses"):
            return pd.read_sql_query("SELECT * FROM survey_responses", conn)

    return pd.DataFrame(columns=RESPONSES_COLUMNS)


def process_input_directory(input_dir: Path, db_path: str, recursive: bool = False) -> tuple[int, int, int]:
    if not input_dir.exists() or not input_dir.is_dir():
        raise NotADirectoryError(f"Папка не найдена: {input_dir}")

    files = list_input_files(input_dir, recursive=recursive)
    if not files:
        print(f"В папке {input_dir} не найдено xls/xlsx файлов")
        return 0, 0, 0

    total_files = 0
    total_rows = 0
    total_inserted = 0

    for file_path in files:
        inserted, rows = append_delta_from_file(file_path, db_path)
        if rows == -1:
            print(f"Пропуск файла (уже обработан): {file_path.name}")
            continue

        total_files += 1
        total_rows += rows
        total_inserted += inserted

    return total_files, total_rows, total_inserted


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ETL для опроса 'Выход в выходной' с дельта-загрузкой")
    parser.add_argument(
        "--input",
        default="Копия 2026-03-24 09.31.24 Выход в выходной.xlsx",
        help="Путь к входному Excel-файлу",
    )
    parser.add_argument(
        "--input-dir",
        help="Папка с выгрузками Excel. Если указана, обрабатываются все новые файлы из папки.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Искать файлы рекурсивно по вложенным папкам (работает вместе с --input-dir)",
    )
    parser.add_argument(
        "--db",
        default="survey_results.db",
        help="Путь к SQLite БД",
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()

    if args.input_dir:
        input_dir = Path(args.input_dir)
        total_files, total_rows, total_inserted = process_input_directory(
            input_dir=input_dir,
            db_path=args.db,
            recursive=args.recursive,
        )
        print("Итог по папке:")
        print(f"Обработано файлов: {total_files}")
        print(f"Строк в выгрузках: {total_rows}")
        print(f"Загружено новых записей: {total_inserted}")
        print(f"БД: {args.db}")
        return

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Файл не найден: {input_path}")

    inserted, rows = append_delta_from_file(input_path, args.db)
    if rows == -1:
        print("Файл уже был обработан ранее, новые записи не загружались.")
    print(f"БД: {args.db}")


if __name__ == "__main__":
    main()
