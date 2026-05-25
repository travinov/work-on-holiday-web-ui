from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

try:
    from src.app_request_state import STATUS_LABELS, VALID_STATUSES, ensure_app_tables
    from src.etl_processor import append_delta_from_file
except ModuleNotFoundError:
    from app_request_state import STATUS_LABELS, VALID_STATUSES, ensure_app_tables
    from etl_processor import append_delta_from_file

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
DB_PATH = BASE_DIR / "survey_results.db"
UPLOAD_DIR = BASE_DIR / "generated_exports"
REPORTS_DIR = BASE_DIR / "reports"
TEMPLATES_DIR = BASE_DIR / "templates"
SUPERUSER_LOGIN_ENV = "WORK_ON_HOLIDAY_SUPERUSER_LOGIN"
SUPERUSER_PASSWORD_ENV = "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD"
SECURE_COOKIES_ENV = "WORK_ON_HOLIDAY_SECURE_COOKIES"
SUPERUSER_COOKIE_NAME = "woh_superuser"
EMPLOYEE_TOKEN_COOKIE_NAME = "woh_employee_token"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

TIME_RANGE_PATTERN = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})\s*$")

app = FastAPI(title="Work On Holiday - Web UI")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_app_tables_for_app() -> None:
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    ensure_app_tables_for_app()


def run_script(script_name: str, args: list[str]) -> str:
    cmd = [sys.executable, str(SRC_DIR / script_name), *args]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Неизвестная ошибка")
    return (completed.stdout or "").strip()


def secure_cookies_enabled() -> bool:
    return os.getenv(SECURE_COOKIES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def set_app_cookie(response: Any, key: str, value: str) -> None:
    response.set_cookie(
        key,
        value,
        httponly=True,
        samesite="lax",
        secure=secure_cookies_enabled(),
    )


def get_last_weekend(today: date) -> tuple[date, date]:
    days_since_sunday = (today.weekday() + 1) % 7
    if days_since_sunday == 0:
        days_since_sunday = 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_saturday = last_sunday - timedelta(days=1)
    return last_saturday, last_sunday


def get_current_week(today: date) -> tuple[date, date]:
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def read_db_stats() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"responses": 0, "processed_files": 0, "managed_requests": 0, "locked_requests": 0, "period_locks": 0}

    with get_db_connection() as conn:
        responses = conn.execute("SELECT COUNT(*) FROM survey_responses").fetchone()[0]
        processed_files = conn.execute("SELECT COUNT(*) FROM ingestion_files").fetchone()[0]
        managed_requests = conn.execute("SELECT COUNT(*) FROM app_request_state").fetchone()[0]
        locked_requests = conn.execute("SELECT COUNT(*) FROM app_report_lock").fetchone()[0]
        period_locks = conn.execute("SELECT COUNT(*) FROM app_period_lock").fetchone()[0]

    return {
        "responses": int(responses),
        "processed_files": int(processed_files),
        "managed_requests": int(managed_requests),
        "locked_requests": int(locked_requests),
        "period_locks": int(period_locks),
    }


def list_reports() -> list[dict[str, str]]:
    files = [p for p in REPORTS_DIR.glob("*.xlsx") if p.is_file() and not p.name.startswith("~$")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    result: list[dict[str, str]] = []
    for file_path in files[:30]:
        result.append(
            {
                "name": file_path.name,
                "modified": datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
            }
        )
    return result


def to_ru_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def to_ru_weekday(value: str | None) -> str:
    if not value:
        return ""
    try:
        weekday_index = datetime.strptime(value, "%Y-%m-%d").weekday()
    except ValueError:
        return ""
    weekdays = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ]
    return weekdays[weekday_index]


def validate_time_range(value: str) -> bool:
    match = TIME_RANGE_PATTERN.match(value)
    if not match:
        return False
    h1, m1, h2, m2 = map(int, match.groups())
    return h1 <= 23 and h2 <= 23 and m1 <= 59 and m2 <= 59


def normalize_systems_text(value: str) -> str:
    parts = [part.strip() for part in re.split(r"\n|\|", value or "") if part.strip()]
    return " | ".join(parts)


def split_systems(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n|\|", value or "") if part.strip()]


def normalize_name_key(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return None
    return cleaned.lower().replace("ё", "е")


def get_superuser_login() -> str:
    return os.getenv(SUPERUSER_LOGIN_ENV, "").strip()


def get_superuser_password() -> str:
    return os.getenv(SUPERUSER_PASSWORD_ENV, "").strip()


def superuser_auth_configured() -> bool:
    return bool(get_superuser_login() and get_superuser_password())


def build_superuser_cookie_value(login: str, password: str) -> str:
    raw_value = f"{login}:{password}"
    return hmac.new(raw_value.encode("utf-8"), b"work-on-holiday-superuser", hashlib.sha256).hexdigest()


def is_superuser_authenticated(request: Request) -> bool:
    login = get_superuser_login()
    password = get_superuser_password()
    cookie_value = request.cookies.get(SUPERUSER_COOKIE_NAME, "")
    if not login or not password or not cookie_value:
        return False
    expected = build_superuser_cookie_value(login, password)
    return hmac.compare_digest(cookie_value, expected)


def redirect_with_message(path: str, msg: str, level: str = "info") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{separator}msg={msg}&level={level}", status_code=303)


def hash_employee_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_employee_token() -> str:
    return secrets.token_urlsafe(18)


def get_employee_token_record_by_hash(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT full_name_key, token_hash, token_issued_at, token_reissued_at, forgot_requested_at, updated_at
        FROM app_employee_auth
        WHERE token_hash = ?;
        """,
        (token_hash,),
    ).fetchone()


def get_employee_token_record(conn: sqlite3.Connection, full_name_key: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT full_name_key, token_hash, token_issued_at, token_reissued_at, forgot_requested_at, updated_at
        FROM app_employee_auth
        WHERE full_name_key = ?;
        """,
        (full_name_key,),
    ).fetchone()


def resolve_employee_by_name(conn: sqlite3.Connection, full_name: str) -> dict[str, str] | None:
    normalized = normalize_name_key(full_name)
    if not normalized:
        return None
    row = conn.execute(
        """
        SELECT DISTINCT
            r.full_name_key,
            COALESCE(r.full_name_normalized, r.full_name) AS full_name
        FROM survey_responses r
        WHERE r.request_type = 'Подать заявку'
          AND r.full_name_key = ?
        ORDER BY full_name
        LIMIT 1;
        """,
        (normalized,),
    ).fetchone()
    if not row:
        return None
    return {"employee_key": row["full_name_key"], "full_name": row["full_name"]}


def upsert_employee_token(conn: sqlite3.Connection, full_name_key: str, token: str, reissued: bool = False) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    token_hash = hash_employee_token(token)
    conn.execute(
        """
        INSERT INTO app_employee_auth (
            full_name_key, token_hash, token_issued_at, token_reissued_at, forgot_requested_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(full_name_key) DO UPDATE SET
            token_hash = excluded.token_hash,
            token_issued_at = CASE
                WHEN app_employee_auth.token_issued_at IS NULL OR app_employee_auth.token_issued_at = ''
                THEN excluded.token_issued_at
                ELSE app_employee_auth.token_issued_at
            END,
            token_reissued_at = excluded.token_reissued_at,
            forgot_requested_at = excluded.forgot_requested_at,
            updated_at = excluded.updated_at;
        """,
        (full_name_key, token_hash, now, now if reissued else None, None, now),
    )


def mark_employee_forgot_token(conn: sqlite3.Connection, full_name_key: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE app_employee_auth
        SET forgot_requested_at = ?, updated_at = ?
        WHERE full_name_key = ?;
        """,
        (now, now, full_name_key),
    )


def clear_employee_forgot_token(conn: sqlite3.Connection, full_name_key: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE app_employee_auth
        SET forgot_requested_at = NULL, updated_at = ?
        WHERE full_name_key = ?;
        """,
        (now, full_name_key),
    )


def get_employee_profile(conn: sqlite3.Connection, full_name_key: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            full_name_key,
            grade_12_plus,
            is_admin,
            is_superuser,
            employee_status,
            status_reason,
            blocked_at,
            archived_at,
            restored_at,
            updated_by,
            updated_at
        FROM app_employee_profile
        WHERE full_name_key = ?;
        """,
        (full_name_key,),
    ).fetchone()
    if not row:
        return {
            "full_name_key": full_name_key,
            "grade_12_plus": 0,
            "is_admin": 0,
            "is_superuser": 0,
            "employee_status": "active",
            "status_reason": None,
            "blocked_at": None,
            "archived_at": None,
            "restored_at": None,
            "updated_by": None,
            "updated_at": None,
        }
    return dict(row)


def upsert_employee_grade_12_plus(conn: sqlite3.Connection, full_name_key: str, grade_12_plus: bool) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO app_employee_profile (full_name_key, grade_12_plus, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(full_name_key) DO UPDATE SET
            grade_12_plus = excluded.grade_12_plus,
            updated_at = excluded.updated_at;
        """,
        (full_name_key, 1 if grade_12_plus else 0, now),
    )


def ensure_employee_profile_row(conn: sqlite3.Connection, full_name_key: str) -> None:
    profile = get_employee_profile(conn, full_name_key)
    if profile["updated_at"] is None:
        upsert_employee_grade_12_plus(conn, full_name_key, False)


def update_employee_admin_role(conn: sqlite3.Connection, full_name_key: str, is_admin: bool, updated_by: str) -> None:
    ensure_employee_profile_row(conn, full_name_key)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE app_employee_profile
        SET is_admin = ?, updated_by = ?, updated_at = ?
        WHERE full_name_key = ?;
        """,
        (1 if is_admin else 0, updated_by, now, full_name_key),
    )


def update_employee_status(conn: sqlite3.Connection, full_name_key: str, status: str, reason: str, updated_by: str) -> None:
    if status not in {"active", "blocked", "archived"}:
        raise ValueError("Некорректный статус пользователя")
    ensure_employee_profile_row(conn, full_name_key)
    now = datetime.now().isoformat(timespec="seconds")
    updates = {
        "employee_status": status,
        "status_reason": reason.strip() or None,
        "blocked_at": None,
        "archived_at": None,
        "restored_at": now if status == "active" else None,
        "updated_by": updated_by,
        "updated_at": now,
    }
    if status == "blocked":
        updates["blocked_at"] = now
    if status == "archived":
        updates["archived_at"] = now
    conn.execute(
        """
        UPDATE app_employee_profile
        SET employee_status = :employee_status,
            status_reason = :status_reason,
            blocked_at = :blocked_at,
            archived_at = :archived_at,
            restored_at = :restored_at,
            updated_by = :updated_by,
            updated_at = :updated_at
        WHERE full_name_key = :full_name_key;
        """,
        {**updates, "full_name_key": full_name_key},
    )


def is_employee_profile_active(profile: dict[str, Any]) -> bool:
    return (profile.get("employee_status") or "active") == "active"


def authenticate_employee_by_token(request: Request) -> dict[str, Any] | None:
    raw_token = request.cookies.get(EMPLOYEE_TOKEN_COOKIE_NAME, "").strip()
    if not raw_token:
        return None
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        token_row = get_employee_token_record_by_hash(conn, hash_employee_token(raw_token))
        if not token_row:
            return None
        employee_row = conn.execute(
            """
            SELECT DISTINCT
                r.full_name_key,
                COALESCE(r.full_name_normalized, r.full_name) AS full_name
            FROM survey_responses r
            WHERE r.request_type = 'Подать заявку'
              AND r.full_name_key = ?
            ORDER BY full_name
            LIMIT 1;
            """,
            (token_row["full_name_key"],),
        ).fetchone()
        profile = get_employee_profile(conn, token_row["full_name_key"])
    if not employee_row or not is_employee_profile_active(profile):
        return None
    return {
        "employee_key": employee_row["full_name_key"],
        "full_name": employee_row["full_name"],
        "is_admin": int(profile.get("is_admin") or 0),
        "is_superuser": int(profile.get("is_superuser") or 0),
        "employee_status": profile.get("employee_status") or "active",
    }


def get_superuser_session(request: Request) -> dict[str, Any] | None:
    if not is_superuser_authenticated(request):
        return None
    return {
        "employee_key": "__superuser__",
        "full_name": "Суперпользователь",
        "is_admin": 1,
        "is_superuser": 1,
        "employee_status": "active",
    }


def get_admin_session(request: Request) -> dict[str, Any] | None:
    superuser_session = get_superuser_session(request)
    if superuser_session:
        return superuser_session
    employee_session = authenticate_employee_by_token(request)
    if employee_session and int(employee_session.get("is_admin") or 0):
        return employee_session
    return None


def is_admin_or_superuser_request(request: Request) -> bool:
    return get_admin_session(request) is not None


def get_admin_actor_key(request: Request) -> str:
    session = get_admin_session(request)
    if not session:
        return "unknown"
    return str(session["employee_key"])


def get_locked_response_ids() -> set[int]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT response_id FROM app_report_lock").fetchall()
    return {int(row["response_id"]) for row in rows}


def get_effective_planned_response_ids(date_from: str, date_to: str) -> list[int]:
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        rows = conn.execute(
            """
            SELECT r.response_id
            FROM survey_responses r
            LEFT JOIN app_request_state st ON st.response_id = r.response_id
            WHERE r.request_type = 'Подать заявку'
              AND COALESCE(st.override_planned_work_date, r.planned_work_date) BETWEEN ? AND ?
            ORDER BY COALESCE(st.override_planned_work_date, r.planned_work_date), r.response_id;
            """,
            (date_from, date_to),
        ).fetchall()
    return [int(row["response_id"]) for row in rows]


def lock_reporting_week(date_from: str, date_to: str, report_file: str) -> int:
    response_ids = get_effective_planned_response_ids(date_from, date_to)
    if not response_ids:
        return 0

    locked_at = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        for response_id in response_ids:
            conn.execute(
                """
                INSERT INTO app_report_lock (response_id, week_start, week_end, report_file, locked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(response_id) DO UPDATE SET
                    week_start = excluded.week_start,
                    week_end = excluded.week_end,
                    report_file = excluded.report_file,
                    locked_at = excluded.locked_at;
                """,
                (response_id, date_from, date_to, report_file, locked_at),
            )
        conn.commit()
    return len(response_ids)


def get_lock_info(conn: sqlite3.Connection, response_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT response_id, week_start, week_end, report_file, locked_at
        FROM app_report_lock
        WHERE response_id = ?;
        """,
        (response_id,),
    ).fetchone()
    return dict(row) if row else None


def get_lock_info_for_date(conn: sqlite3.Connection, planned_date: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT week_start, week_end, report_file, locked_at
        FROM app_report_lock
        WHERE ? BETWEEN week_start AND week_end
        ORDER BY locked_at DESC
        LIMIT 1;
        """,
        (planned_date,),
    ).fetchone()
    return dict(row) if row else None


def create_period_lock(
    conn: sqlite3.Connection,
    *,
    lock_type: str,
    date_from: str,
    date_to: str,
    created_by: str,
    comment: str = "",
) -> None:
    if lock_type not in {"planning", "actual"}:
        raise ValueError("Некорректный тип блокировки")
    parsed_from = datetime.strptime(date_from, "%Y-%m-%d").date()
    parsed_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    if parsed_from > parsed_to:
        raise ValueError("Дата начала позже даты окончания")
    conn.execute(
        """
        INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        (lock_type, date_from, date_to, created_by, datetime.now().isoformat(timespec="seconds"), comment.strip() or None),
    )


def get_period_lock_for_date(conn: sqlite3.Connection, lock_type: str, target_date: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT lock_id, lock_type, date_from, date_to, created_by, created_at, comment
        FROM app_period_lock
        WHERE lock_type = ?
          AND ? BETWEEN date_from AND date_to
        ORDER BY created_at DESC, lock_id DESC
        LIMIT 1;
        """,
        (lock_type, target_date),
    ).fetchone()
    return dict(row) if row else None


def get_period_locks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT lock_id, lock_type, date_from, date_to, created_by, created_at, comment
        FROM app_period_lock
        ORDER BY date_from DESC, created_at DESC
        LIMIT 30;
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["date_from_ru"] = to_ru_date(item["date_from"])
        item["date_to_ru"] = to_ru_date(item["date_to"])
        item["lock_type_label"] = "Прием заявок" if item["lock_type"] == "planning" else "Факт"
        result.append(item)
    return result


def get_employee_display_name(conn: sqlite3.Connection, employee_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT COALESCE(full_name_normalized, full_name) AS full_name
        FROM survey_responses
        WHERE request_type = 'Подать заявку'
          AND full_name_key = ?
        ORDER BY response_id DESC
        LIMIT 1;
        """,
        (employee_key,),
    ).fetchone()
    return row["full_name"] if row else None


def get_next_response_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(response_id), 0) + 1 AS next_id FROM survey_responses").fetchone()
    return int(row["next_id"])


def get_employee_list() -> list[dict[str, str]]:
    if not DB_PATH.exists():
        return []

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT
                r.full_name_key,
                COALESCE(r.full_name_normalized, r.full_name) AS full_name
            FROM survey_responses r
            WHERE r.request_type = 'Подать заявку'
              AND r.full_name_key IS NOT NULL
            ORDER BY full_name;
            """
        ).fetchall()

    return [{"employee_key": row["full_name_key"], "full_name": row["full_name"]} for row in rows]


def get_admin_test_data_employees() -> list[dict[str, Any]]:
    return [
        employee
        for employee in get_admin_employees_overview()
        if employee.get("employee_status", "active") == "active"
    ]


def get_admin_employees_overview() -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        rows = conn.execute(
            """
            SELECT
                e.full_name_key AS employee_key,
                e.full_name,
                COUNT(DISTINCT r.response_id) AS requests_count,
                auth.token_issued_at,
                auth.token_reissued_at,
                auth.forgot_requested_at,
                COALESCE(profile.grade_12_plus, 0) AS grade_12_plus,
                COALESCE(profile.is_admin, 0) AS is_admin,
                COALESCE(profile.is_superuser, 0) AS is_superuser,
                COALESCE(profile.employee_status, 'active') AS employee_status,
                profile.status_reason,
                profile.blocked_at,
                profile.archived_at,
                profile.restored_at,
                profile.updated_by,
                profile.updated_at AS profile_updated_at
            FROM (
                SELECT DISTINCT
                    r.full_name_key,
                    COALESCE(r.full_name_normalized, r.full_name) AS full_name
                FROM survey_responses r
                WHERE r.request_type = 'Подать заявку'
                  AND r.full_name_key IS NOT NULL
            ) e
            LEFT JOIN survey_responses r
                ON r.full_name_key = e.full_name_key
               AND r.request_type = 'Подать заявку'
            LEFT JOIN app_employee_auth auth
                ON auth.full_name_key = e.full_name_key
            LEFT JOIN app_employee_profile profile
                ON profile.full_name_key = e.full_name_key
            GROUP BY
                e.full_name_key,
                e.full_name,
                auth.token_issued_at,
                auth.token_reissued_at,
                auth.forgot_requested_at,
                profile.grade_12_plus,
                profile.is_admin,
                profile.is_superuser,
                profile.employee_status,
                profile.status_reason,
                profile.blocked_at,
                profile.archived_at,
                profile.restored_at,
                profile.updated_by,
                profile.updated_at
            ORDER BY e.full_name;
            """
        ).fetchall()
    return [dict(row) for row in rows]


def delete_admin_test_data_for_date(conn: sqlite3.Connection, planned_work_date: str) -> int:
    rows = conn.execute(
        """
        SELECT response_id
        FROM survey_responses
        WHERE planned_work_date = ?
          AND source_file LIKE ?;
        """,
        (planned_work_date, f"admin_test_data:{planned_work_date}:%"),
    ).fetchall()
    response_ids = [int(row["response_id"]) for row in rows]
    for response_id in response_ids:
        conn.execute("DELETE FROM app_request_state WHERE response_id = ?;", (response_id,))
        conn.execute("DELETE FROM app_report_lock WHERE response_id = ?;", (response_id,))
        conn.execute("DELETE FROM response_systems WHERE response_id = ?;", (response_id,))
        conn.execute("DELETE FROM survey_responses WHERE response_id = ?;", (response_id,))
    return len(response_ids)


def insert_test_data_request(
    conn: sqlite3.Connection,
    *,
    employee_key: str,
    planned_work_date: str,
    planned_work_time: str,
    payment_type: str,
    task_description: str,
    justification: str,
    systems: list[str],
    source_file: str,
) -> int:
    full_name = get_employee_display_name(conn, employee_key)
    if not full_name:
        raise ValueError(f"Сотрудник не найден: {employee_key}")

    employee_profile = get_employee_profile(conn, employee_key)
    if not is_employee_profile_active(employee_profile):
        raise ValueError(f"Профиль сотрудника неактивен: {full_name}")
    if employee_profile["grade_12_plus"] and payment_type == "Двойная оплата":
        raise ValueError(f"Двойная оплата недоступна для сотрудника с грейдом 12+: {full_name}")

    response_id = get_next_response_id(conn)
    now = datetime.now().isoformat(timespec="seconds")
    system_columns = [None] * 6
    for index, system_name in enumerate(systems[:6]):
        system_columns[index] = system_name

    row_hash = hashlib.sha256(
        "|".join(
            [
                source_file,
                employee_key,
                planned_work_date,
                planned_work_time,
                payment_type,
                task_description,
                justification,
                normalize_systems_text(" | ".join(systems)),
            ]
        ).encode("utf-8")
    ).hexdigest()

    conn.execute(
        """
        INSERT INTO survey_responses (
            response_id,
            source_row,
            start_time,
            full_name,
            full_name_normalized,
            full_name_key,
            request_type,
            grade_12_plus,
            payment_type,
            task_description,
            justification,
            planned_work_date,
            planned_work_time,
            target_work_date,
            need_additional_system_1,
            need_additional_system_2,
            need_additional_system_3,
            need_additional_system_4,
            need_additional_system_5,
            system_1,
            system_2,
            system_3,
            system_4,
            system_5,
            system_6,
            row_hash,
            source_file,
            loaded_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'Подать заявку', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            response_id,
            response_id,
            now,
            full_name,
            full_name,
            employee_key,
            int(employee_profile["grade_12_plus"]),
            payment_type,
            task_description,
            justification,
            planned_work_date,
            planned_work_time,
            planned_work_date,
            1 if len(systems) > 1 else 0,
            1 if len(systems) > 2 else 0,
            1 if len(systems) > 3 else 0,
            1 if len(systems) > 4 else 0,
            1 if len(systems) > 5 else 0,
            *system_columns,
            row_hash,
            source_file,
            now,
        ),
    )
    conn.execute("DELETE FROM response_systems WHERE response_id = ?;", (response_id,))
    for system_order, system_name in enumerate(systems, start=1):
        conn.execute(
            "INSERT INTO response_systems (response_id, system_order, system_name) VALUES (?, ?, ?);",
            (response_id, system_order, system_name),
        )
    return response_id


def get_admin_requests_overview() -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        rows = conn.execute(
            """
            WITH planned_systems AS (
                SELECT response_id, GROUP_CONCAT(system_name, ' | ') AS systems
                FROM response_systems
                GROUP BY response_id
            )
            SELECT
                r.response_id,
                r.full_name_key AS employee_key,
                COALESCE(r.full_name_normalized, r.full_name) AS full_name,
                COALESCE(st.override_planned_work_date, r.planned_work_date) AS planned_work_date,
                COALESCE(st.override_planned_work_time, r.planned_work_time) AS planned_work_time,
                COALESCE(st.override_payment_type, r.payment_type) AS payment_type,
                COALESCE(st.override_task_description, r.task_description) AS task_description,
                COALESCE(st.status, 'active') AS request_status,
                st.is_corrected,
                st.actual_work_date,
                st.actual_work_time,
                lock.week_start,
                lock.week_end,
                lock.locked_at,
                ps.systems
            FROM survey_responses r
            LEFT JOIN app_request_state st ON st.response_id = r.response_id
            LEFT JOIN app_report_lock lock ON lock.response_id = r.response_id
            LEFT JOIN planned_systems ps ON ps.response_id = r.response_id
            WHERE r.request_type = 'Подать заявку'
            ORDER BY COALESCE(st.override_planned_work_date, r.planned_work_date) DESC, full_name;
            """
        ).fetchall()

    result: list[dict[str, Any]] = []
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        for row in rows:
            item = dict(row)
            status = item.get("request_status") or "active"
            status_label = STATUS_LABELS.get(status, status)
            if item.get("is_corrected"):
                status_label += " · откорректирована"
            planning_lock = (
                get_period_lock_for_date(conn, "planning", item.get("planned_work_date"))
                if item.get("planned_work_date")
                else None
            )
            actual_lock = (
                get_period_lock_for_date(conn, "actual", item.get("actual_work_date"))
                if item.get("actual_work_date")
                else None
            )
            item["status_label"] = status_label
            item["planned_work_date_ru"] = to_ru_date(item.get("planned_work_date"))
            item["lock_week_label"] = (
                f"{to_ru_date(item['week_start'])} - {to_ru_date(item['week_end'])}"
                if item.get("week_start") and item.get("week_end")
                else ""
            )
            item["planning_lock_label"] = (
                f"{to_ru_date(planning_lock['date_from'])} - {to_ru_date(planning_lock['date_to'])}"
                if planning_lock
                else ""
            )
            item["actual_lock_label"] = (
                f"{to_ru_date(actual_lock['date_from'])} - {to_ru_date(actual_lock['date_to'])}"
                if actual_lock
                else ""
            )
            result.append(item)
    return result


def get_employee_requests(employee_key: str, admin_mode: bool = False) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        rows = conn.execute(
            """
            WITH planned_rows AS (
                SELECT
                    r.*
                FROM survey_responses r
                WHERE r.request_type = 'Подать заявку'
                  AND r.full_name_key = ?
                  AND r.planned_work_date IS NOT NULL
            ),
            planned_systems AS (
                SELECT
                    p.response_id,
                    GROUP_CONCAT(s.system_name, ' | ') AS systems
                FROM planned_rows p
                LEFT JOIN response_systems s ON s.response_id = p.response_id
                GROUP BY p.response_id
            )
            SELECT
                p.response_id,
                p.full_name_key,
                COALESCE(p.full_name_normalized, p.full_name) AS full_name,
                p.planned_work_date,
                p.planned_work_time,
                p.payment_type,
                p.task_description,
                p.justification,
                ps.systems,
                ('req:' || CAST(p.response_id AS TEXT)) AS request_uid,
                st.status,
                st.is_corrected,
                st.override_planned_work_date,
                st.override_planned_work_time,
                st.override_payment_type,
                st.override_task_description,
                st.override_justification,
                st.override_systems,
                st.actual_work_date AS state_actual_work_date,
                st.actual_work_time AS state_actual_work_time,
                st.corrected_at
            FROM planned_rows p
            LEFT JOIN planned_systems ps ON ps.response_id = p.response_id
            LEFT JOIN app_request_state st
                ON st.request_uid = ('req:' || CAST(p.response_id AS TEXT))
            ORDER BY p.planned_work_date DESC;
            """,
            (employee_key,),
        ).fetchall()

    result: list[dict[str, Any]] = []
    with get_db_connection() as conn:
        for row in rows:
            row_dict = dict(row)
            effective_planned_date = row_dict["override_planned_work_date"] or row_dict["planned_work_date"]
            effective_planned_time = row_dict["override_planned_work_time"] or row_dict["planned_work_time"]
            effective_payment = row_dict["override_payment_type"] or row_dict["payment_type"]
            effective_task = row_dict["override_task_description"] or row_dict["task_description"]
            effective_justification = row_dict["override_justification"] or row_dict["justification"]
            effective_systems = row_dict["override_systems"] or row_dict["systems"] or ""
            effective_actual_date = row_dict["state_actual_work_date"]
            effective_actual_time = row_dict["state_actual_work_time"]
            status = row_dict["status"]
            if status not in VALID_STATUSES:
                status = "active"
            is_corrected = bool(row_dict.get("is_corrected") or 0)
            status_label = STATUS_LABELS.get(status, "Неизвестно")
            if is_corrected:
                status_label += " · заявка откорректирована"
            lock_info = get_lock_info(conn, int(row_dict["response_id"]))
            planning_lock = (
                get_period_lock_for_date(conn, "planning", effective_planned_date)
                if effective_planned_date
                else None
            )
            actual_lock = (
                get_period_lock_for_date(conn, "actual", effective_actual_date)
                if effective_actual_date
                else None
            )
            is_locked = lock_info is not None or planning_lock is not None or actual_lock is not None
            can_edit = admin_mode or not is_locked

            result.append(
                {
                    "request_uid": row_dict["request_uid"],
                    "response_id": row_dict["response_id"],
                    "full_name": row_dict["full_name"],
                    "employee_key": row_dict["full_name_key"],
                    "status": status,
                    "status_label": status_label,
                    "is_corrected": is_corrected,
                    "corrected_at": to_ru_date((row_dict["corrected_at"] or "")[:10]) if row_dict["corrected_at"] else "",
                    "planned_work_date_iso": effective_planned_date or "",
                    "planned_work_date_ru": to_ru_date(effective_planned_date),
                    "planned_work_weekday_ru": to_ru_weekday(effective_planned_date),
                    "planned_work_time": effective_planned_time or "",
                    "payment_type": effective_payment or "",
                    "task_description": effective_task or "",
                    "justification": effective_justification or "",
                    "systems": effective_systems,
                    "systems_multiline": "\n".join([p.strip() for p in effective_systems.split("|") if p.strip()]),
                    "actual_work_date_iso": effective_actual_date or "",
                    "actual_work_date_ru": to_ru_date(effective_actual_date),
                    "actual_work_time": effective_actual_time or "",
                    "is_locked": is_locked,
                    "planning_lock_label": (
                        f"{to_ru_date(planning_lock['date_from'])} - {to_ru_date(planning_lock['date_to'])}"
                        if planning_lock
                        else ""
                    ),
                    "actual_lock_label": (
                        f"{to_ru_date(actual_lock['date_from'])} - {to_ru_date(actual_lock['date_to'])}"
                        if actual_lock
                        else ""
                    ),
                    "can_edit": can_edit,
                    "lock_week_label": (
                        f"{to_ru_date(lock_info['week_start'])} - {to_ru_date(lock_info['week_end'])}"
                        if lock_info
                        else ""
                    ),
                }
            )

    return result


def get_request_state(conn: sqlite3.Connection, request_uid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM app_request_state WHERE request_uid = ?", (request_uid,)).fetchone()
    return dict(row) if row else None


def upsert_request_state(
    conn: sqlite3.Connection,
    *,
    request_uid: str,
    response_id: int,
    full_name_key: str,
    updates: dict[str, Any],
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    existing = get_request_state(conn, request_uid)

    state = {
        "request_uid": request_uid,
        "response_id": int(response_id),
        "full_name_key": full_name_key,
        "status": "active",
        "is_corrected": 0,
        "override_planned_work_date": None,
        "override_planned_work_time": None,
        "override_payment_type": None,
        "override_task_description": None,
        "override_justification": None,
        "override_systems": None,
        "actual_work_date": None,
        "actual_work_time": None,
        "corrected_at": None,
        "created_at": now,
        "updated_at": now,
    }

    if existing:
        state.update(existing)

    state.update(updates)
    state["response_id"] = int(response_id)
    state["full_name_key"] = full_name_key
    state["updated_at"] = now

    conn.execute(
        """
        INSERT INTO app_request_state (
            request_uid,
            response_id,
            full_name_key,
            status,
            is_corrected,
            override_planned_work_date,
            override_planned_work_time,
            override_payment_type,
            override_task_description,
            override_justification,
            override_systems,
            actual_work_date,
            actual_work_time,
            corrected_at,
            created_at,
            updated_at
        ) VALUES (
            :request_uid,
            :response_id,
            :full_name_key,
            :status,
            :is_corrected,
            :override_planned_work_date,
            :override_planned_work_time,
            :override_payment_type,
            :override_task_description,
            :override_justification,
            :override_systems,
            :actual_work_date,
            :actual_work_time,
            :corrected_at,
            :created_at,
            :updated_at
        )
        ON CONFLICT(request_uid) DO UPDATE SET
            response_id = excluded.response_id,
            full_name_key = excluded.full_name_key,
            status = excluded.status,
            is_corrected = excluded.is_corrected,
            override_planned_work_date = excluded.override_planned_work_date,
            override_planned_work_time = excluded.override_planned_work_time,
            override_payment_type = excluded.override_payment_type,
            override_task_description = excluded.override_task_description,
            override_justification = excluded.override_justification,
            override_systems = excluded.override_systems,
            actual_work_date = excluded.actual_work_date,
            actual_work_time = excluded.actual_work_time,
            corrected_at = excluded.corrected_at,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at;
        """,
        state,
    )


def get_request_identity(conn: sqlite3.Connection, employee_key: str, response_id: int) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT full_name_key
        FROM survey_responses
        WHERE response_id = ?
          AND request_type = 'Подать заявку'
          AND full_name_key = ?;
        """,
        (response_id, employee_key),
    ).fetchone()
    if not row:
        return None
    return f"req:{response_id}", row["full_name_key"]


def get_authenticated_employee_key(request: Request) -> str | None:
    employee_session = authenticate_employee_by_token(request)
    return employee_session["employee_key"] if employee_session else None


def build_employee_redirect(employee_key: str, msg: str, level: str, admin_mode: bool = False) -> RedirectResponse:
    admin_suffix = "&admin_mode=1" if admin_mode else ""
    return RedirectResponse(
        url=f"/employee?employee_key={employee_key}&msg={msg}&level={level}{admin_suffix}",
        status_code=303,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str | None = None, level: str = "info") -> HTMLResponse:
    admin_session = get_admin_session(request)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "request": request,
            "msg": msg,
            "level": level,
            "is_admin": admin_session is not None,
            "is_superuser": get_superuser_session(request) is not None,
            "superuser_auth_configured": superuser_auth_configured(),
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, msg: str | None = None, level: str = "info") -> HTMLResponse:
    admin_session = get_admin_session(request)
    if not admin_session:
        return RedirectResponse(url="/?msg=Кабинет администратора доступен только администратору&level=error", status_code=303)

    weekend_from, weekend_to = get_last_weekend(date.today())
    week_from, week_to = get_current_week(date.today())
    context = {
        "request": request,
        "msg": msg,
        "level": level,
        "is_admin": True,
        "is_superuser": int(admin_session.get("is_superuser") or 0),
        "admin_session": admin_session,
        "superuser_auth_configured": superuser_auth_configured(),
        "stats": read_db_stats(),
        "reports": list_reports(),
        "period_locks": [],
        "default_from": weekend_from.isoformat(),
        "default_to": weekend_to.isoformat(),
        "default_week_from": week_from.isoformat(),
        "default_week_to": week_to.isoformat(),
    }
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        context["period_locks"] = get_period_locks(conn)
    return templates.TemplateResponse(request, "index.html", context)


@app.post("/superuser/login")
def superuser_login(superuser_login: str = Form(...), superuser_password: str = Form(...)) -> RedirectResponse:
    configured_login = get_superuser_login()
    configured_password = get_superuser_password()
    if not configured_login or not configured_password:
        return RedirectResponse(url="/?msg=Суперпользователь не настроен в окружении&level=error", status_code=303)
    if not hmac.compare_digest(superuser_login.strip(), configured_login) or not hmac.compare_digest(
        superuser_password,
        configured_password,
    ):
        return RedirectResponse(url="/?msg=Неверный логин или пароль суперпользователя&level=error", status_code=303)

    response = RedirectResponse(url="/admin?msg=Вход суперпользователя выполнен&level=success", status_code=303)
    set_app_cookie(response, SUPERUSER_COOKIE_NAME, build_superuser_cookie_value(configured_login, configured_password))
    return response


@app.post("/superuser/logout")
def superuser_logout() -> RedirectResponse:
    response = RedirectResponse(url="/?msg=Сессия суперпользователя завершена&level=info", status_code=303)
    response.delete_cookie(SUPERUSER_COOKIE_NAME)
    return response


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, msg: str | None = None, level: str = "info") -> HTMLResponse:
    admin_session = get_admin_session(request)
    if not admin_session:
        return RedirectResponse(url="/?msg=Раздел пользователей доступен только администратору&level=error", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "users": get_admin_employees_overview(),
            "msg": msg,
            "level": level,
            "request": request,
            "admin_session": admin_session,
            "is_superuser": int(admin_session.get("is_superuser") or 0),
        },
    )


@app.get("/admin/requests", response_class=HTMLResponse)
def admin_requests(request: Request, msg: str | None = None, level: str = "info") -> HTMLResponse:
    admin_session = get_admin_session(request)
    if not admin_session:
        return RedirectResponse(url="/?msg=Раздел заявок доступен только администратору&level=error", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_requests.html",
        {
            "requests_data": get_admin_requests_overview(),
            "msg": msg,
            "level": level,
            "request": request,
            "admin_session": admin_session,
        },
    )


@app.get("/admin/test-data", response_class=HTMLResponse)
def admin_test_data(request: Request, msg: str | None = None, level: str = "info") -> HTMLResponse:
    admin_session = get_admin_session(request)
    if not admin_session:
        return RedirectResponse(
            url="/?msg=Генерация тестовых данных доступна только администратору&level=error",
            status_code=303,
        )
    return templates.TemplateResponse(
        request,
        "admin_test_data.html",
        {
            "request": request,
            "msg": msg,
            "level": level,
            "admin_session": admin_session,
            "employees": get_admin_test_data_employees(),
            "default_work_date": date.today().isoformat(),
            "default_work_time": "10:00 - 14:00",
            "default_task": "сопровождение релиза",
            "default_justification": "технологическое окно",
            "default_systems": "Пуаро | ЕФС.Риск-решения",
        },
    )


@app.post("/admin/test-data")
async def admin_create_test_data(request: Request) -> RedirectResponse:
    admin_session = get_admin_session(request)
    if not admin_session:
        return RedirectResponse(
            url="/?msg=Генерация тестовых данных доступна только администратору&level=error",
            status_code=303,
        )

    form = await request.form()
    test_work_date = str(form.get("test_work_date") or "").strip()
    generation_mode = str(form.get("generation_mode") or "planned").strip()
    planned_work_time = str(form.get("planned_work_time") or "10:00 - 14:00").strip()
    task_description = str(form.get("task_description") or "сопровождение релиза").strip()
    justification = str(form.get("justification") or "технологическое окно").strip()
    systems = split_systems(str(form.get("systems") or "Пуаро | ЕФС.Риск-решения"))
    employee_keys = []
    for raw_key in form.getlist("employee_keys"):
        key = str(raw_key).strip()
        if key and key not in employee_keys:
            employee_keys.append(key)

    try:
        datetime.strptime(test_work_date, "%Y-%m-%d")
    except ValueError:
        return redirect_with_message("/admin/test-data", "Некорректная дата тестовых заявок", "error")

    if generation_mode not in {"planned", "plan_and_actual"}:
        return redirect_with_message("/admin/test-data", "Некорректный режим генерации", "error")
    if not validate_time_range(planned_work_time):
        return redirect_with_message("/admin/test-data", "Некорректный формат времени", "error")
    if not employee_keys:
        return redirect_with_message("/admin/test-data", "Выберите хотя бы одного сотрудника", "error")
    if not systems:
        return redirect_with_message("/admin/test-data", "Укажите хотя бы одну АС", "error")

    employee_map = {employee["employee_key"]: employee for employee in get_admin_test_data_employees()}
    unknown_keys = [key for key in employee_keys if key not in employee_map]
    if unknown_keys:
        return redirect_with_message("/admin/test-data", "Выбран несуществующий или неактивный сотрудник", "error")

    batch_id = datetime.now().strftime("%Y%m%d%H%M%S")
    source_file = f"admin_test_data:{test_work_date}:{batch_id}"
    created_count = 0

    with get_db_connection() as conn:
        ensure_app_tables(conn)
        deleted_count = delete_admin_test_data_for_date(conn, test_work_date)
        for employee_key in employee_keys:
            employee_profile = get_employee_profile(conn, employee_key)
            payment_type = "Отгул" if employee_profile["grade_12_plus"] else "Двойная оплата"
            response_id = insert_test_data_request(
                conn,
                employee_key=employee_key,
                planned_work_date=test_work_date,
                planned_work_time=planned_work_time,
                payment_type=payment_type,
                task_description=task_description,
                justification=justification,
                systems=systems,
                source_file=source_file,
            )
            if generation_mode == "plan_and_actual":
                upsert_request_state(
                    conn,
                    request_uid=f"req:{response_id}",
                    response_id=response_id,
                    full_name_key=employee_key,
                    updates={
                        "status": "completed",
                        "actual_work_date": test_work_date,
                        "actual_work_time": planned_work_time,
                    },
                )
            created_count += 1
        conn.commit()

    mode_label = "с фактом" if generation_mode == "plan_and_actual" else "плановых"
    return redirect_with_message(
        "/admin/test-data",
        f"Создано {created_count} тестовых заявок {mode_label}. Удалено старых за дату: {deleted_count}",
        "success",
    )


@app.get("/employee", response_class=HTMLResponse)
def employee_cabinet(
    request: Request,
    employee_key: str | None = None,
    pending_employee_key: str | None = None,
    pending_employee_name: str | None = None,
    msg: str | None = None,
    level: str = "info",
    admin_mode: int = 0,
) -> HTMLResponse:
    admin_session = get_admin_session(request)
    is_admin = admin_session is not None
    is_admin_mode = bool(admin_mode) and is_admin
    employee_session = None if is_admin_mode else authenticate_employee_by_token(request)
    employees = get_employee_list() if is_admin_mode else []

    if is_admin_mode:
        selected = next((e for e in employees if e["employee_key"] == employee_key), None)
    else:
        selected = employee_session

    active_employee_key = selected["employee_key"] if selected else None
    requests = get_employee_requests(active_employee_key, admin_mode=is_admin_mode) if selected else []

    token_meta = None
    profile = None
    if selected:
        with get_db_connection() as conn:
            ensure_app_tables(conn)
            token_row = get_employee_token_record(conn, selected["employee_key"])
            profile = get_employee_profile(conn, selected["employee_key"])
        if token_row:
            token_meta = {
                "issued_at": token_row["token_issued_at"],
                "reissued_at": token_row["token_reissued_at"],
            }

    return templates.TemplateResponse(
        request,
        "employee.html",
        {
            "employees": employees,
            "selected": selected,
            "requests": requests,
            "msg": msg,
            "level": level,
            "today": date.today().isoformat(),
            "admin_mode": is_admin_mode,
            "is_admin": is_admin,
            "employee_session": employee_session,
            "token_meta": token_meta,
            "profile": profile,
            "pending_employee_key": pending_employee_key,
            "pending_employee_name": pending_employee_name,
        },
    )


@app.post("/employee/login")
def employee_login(
    request: Request,
    full_name: str = Form(""),
    access_token: str = Form(""),
    employee_key: str = Form(""),
    grade_12_plus: str = Form("0"),
):
    full_name = full_name.strip()
    access_token = access_token.strip()
    employee_key = employee_key.strip()
    grade_12_plus_flag = grade_12_plus == "1"

    if not full_name:
        return redirect_with_message("/employee", "Укажите ФИО", "error")

    with get_db_connection() as conn:
        ensure_app_tables(conn)
        employee = resolve_employee_by_name(conn, full_name)
        if not employee:
            return redirect_with_message("/employee", "Сотрудник с таким ФИО не найден", "error")
        profile = get_employee_profile(conn, employee["employee_key"])
        if not is_employee_profile_active(profile):
            if profile.get("employee_status") == "blocked":
                return redirect_with_message("/employee", "Пользователь заблокирован", "error")
            if profile.get("employee_status") == "archived":
                return redirect_with_message("/employee", "Пользователь архивирован", "error")
            return redirect_with_message(
                "/employee",
                f"Профиль сотрудника неактивен: {profile.get('employee_status')}",
                "error",
            )
        token_record = get_employee_token_record(conn, employee["employee_key"])

    if token_record:
        if not access_token:
            return RedirectResponse(
                url=(
                    f"/employee?pending_employee_key={employee['employee_key']}"
                    f"&pending_employee_name={employee['full_name']}"
                    f"&msg=Для этого сотрудника уже выдан токен. Укажите токен или нажмите Забыл токен."
                    f"&level=info"
                ),
                status_code=303,
            )
        if employee_key and employee_key != employee["employee_key"]:
            return redirect_with_message("/employee", "ФИО и токен не совпадают", "error")
        if token_record["token_hash"] != hash_employee_token(access_token):
            return RedirectResponse(
                url=(
                    f"/employee?pending_employee_key={employee['employee_key']}"
                    f"&pending_employee_name={employee['full_name']}"
                    f"&msg=Токен не найден или устарел"
                    f"&level=error"
                ),
                status_code=303,
            )

        with get_db_connection() as conn:
            ensure_app_tables(conn)
            clear_employee_forgot_token(conn, employee["employee_key"])
            conn.commit()

        response = RedirectResponse(url="/employee?msg=Вход выполнен&level=success", status_code=303)
        set_app_cookie(response, EMPLOYEE_TOKEN_COOKIE_NAME, access_token)
        return response

    issued_token = generate_employee_token()
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        upsert_employee_grade_12_plus(conn, employee["employee_key"], grade_12_plus_flag)
        upsert_employee_token(conn, employee["employee_key"], issued_token, reissued=False)
        conn.commit()

    response = templates.TemplateResponse(
        request,
        "employee_token_issued.html",
        {
            "title": "Токен сотрудника создан",
            "token": issued_token,
            "employee_name": employee["full_name"],
            "grade_12_plus": grade_12_plus_flag,
            "return_url": "/employee",
            "is_admin": False,
        },
    )
    set_app_cookie(response, EMPLOYEE_TOKEN_COOKIE_NAME, issued_token)
    return response


@app.post("/admin/employee/grade")
def admin_update_employee_grade(request: Request, employee_key: str = Form(...), grade_12_plus: str = Form("0")) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Изменение грейда доступно только администратору", "error")

    grade_12_plus_flag = grade_12_plus == "1"
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        full_name = get_employee_display_name(conn, employee_key)
        if not full_name:
            return redirect_with_message("/admin/users", "Сотрудник не найден", "error")
        upsert_employee_grade_12_plus(conn, employee_key, grade_12_plus_flag)
        conn.commit()

    return redirect_with_message("/admin/users", "Признак грейда сотрудника обновлен", "success")


@app.post("/admin/employee/admin-role")
def admin_update_employee_admin_role(request: Request, employee_key: str = Form(...), is_admin: str = Form("0")) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Назначение роли доступно только администратору", "error")

    with get_db_connection() as conn:
        ensure_app_tables(conn)
        full_name = get_employee_display_name(conn, employee_key)
        if not full_name:
            return redirect_with_message("/admin/users", "Сотрудник не найден", "error")
        update_employee_admin_role(conn, employee_key, is_admin == "1", get_admin_actor_key(request))
        conn.commit()

    return redirect_with_message("/admin/users", "Роль администратора обновлена", "success")


@app.post("/admin/employee/status")
def admin_update_employee_status(
    request: Request,
    employee_key: str = Form(...),
    employee_status: str = Form(...),
    status_reason: str = Form(""),
) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Изменение статуса доступно только администратору", "error")

    with get_db_connection() as conn:
        ensure_app_tables(conn)
        full_name = get_employee_display_name(conn, employee_key)
        if not full_name:
            return redirect_with_message("/admin/users", "Сотрудник не найден", "error")
        try:
            update_employee_status(conn, employee_key, employee_status, status_reason, get_admin_actor_key(request))
        except ValueError as exc:
            return redirect_with_message("/admin/users", str(exc), "error")
        if employee_status == "archived":
            rows = conn.execute(
                """
                SELECT r.response_id
                FROM survey_responses r
                LEFT JOIN app_request_state st ON st.response_id = r.response_id
                WHERE r.full_name_key = ?
                  AND r.request_type = 'Подать заявку'
                  AND COALESCE(st.status, 'active') = 'active'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM app_period_lock pl
                    WHERE pl.lock_type = 'planning'
                      AND COALESCE(st.override_planned_work_date, r.planned_work_date) BETWEEN pl.date_from AND pl.date_to
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM app_report_lock legacy_lock
                    WHERE legacy_lock.response_id = r.response_id
                  );
                """,
                (employee_key,),
            ).fetchall()
            for row in rows:
                upsert_request_state(
                    conn,
                    request_uid=f"req:{row['response_id']}",
                    response_id=int(row["response_id"]),
                    full_name_key=employee_key,
                    updates={"status": "cancelled"},
                )
        conn.commit()

    return redirect_with_message("/admin/users", "Статус пользователя обновлен", "success")


@app.post("/employee/logout")
def employee_logout() -> RedirectResponse:
    response = RedirectResponse(url="/employee?msg=Выход выполнен&level=info", status_code=303)
    response.delete_cookie(EMPLOYEE_TOKEN_COOKIE_NAME)
    return response


@app.post("/employee/forgot-token")
def employee_forgot_token(full_name: str = Form(...)) -> RedirectResponse:
    full_name = full_name.strip()
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        employee = resolve_employee_by_name(conn, full_name)
        if not employee:
            return redirect_with_message("/employee", "Сотрудник с таким ФИО не найден", "error")
        token_record = get_employee_token_record(conn, employee["employee_key"])
        if not token_record:
            return redirect_with_message("/employee", "Для сотрудника токен еще не создавался", "error")
        mark_employee_forgot_token(conn, employee["employee_key"])
        conn.commit()
    return redirect_with_message("/employee", "Запрос на перевыпуск токена отправлен администратору", "success")


@app.post("/admin/employee/reissue-token")
def admin_reissue_employee_token(request: Request, employee_key: str = Form(...)):
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/employee", "Перевыпуск токена доступен только администратору", "error")

    with get_db_connection() as conn:
        ensure_app_tables(conn)
        row = conn.execute(
            """
            SELECT DISTINCT
                r.full_name_key,
                COALESCE(r.full_name_normalized, r.full_name) AS full_name
            FROM survey_responses r
            WHERE r.request_type = 'Подать заявку'
              AND r.full_name_key = ?
            LIMIT 1;
            """,
            (employee_key,),
        ).fetchone()
        if not row:
            return redirect_with_message("/employee?admin_mode=1", "Сотрудник не найден", "error")
        new_token = generate_employee_token()
        upsert_employee_token(conn, employee_key, new_token, reissued=True)
        clear_employee_forgot_token(conn, employee_key)
        conn.commit()

    return templates.TemplateResponse(
        request,
        "employee_token_issued.html",
        {
            "title": "Токен сотрудника перевыпущен",
            "token": new_token,
            "employee_name": row["full_name"],
            "return_url": f"/employee?admin_mode=1&employee_key={employee_key}",
            "is_admin": True,
        },
    )


@app.post("/employee/request/actual")
def employee_set_actual_time(
    request: Request,
    employee_key: str = Form(...),
    response_id: int = Form(...),
    actual_work_date: str = Form(...),
    actual_work_time: str = Form(...),
    admin_mode: int = Form(0),
) -> RedirectResponse:
    is_admin_mode = bool(admin_mode) and is_admin_or_superuser_request(request)
    authenticated_employee_key = get_authenticated_employee_key(request)
    if not is_admin_mode and authenticated_employee_key != employee_key:
        return build_employee_redirect(employee_key, "Требуется повторный вход по токену", "error", admin_mode=False)
    if not actual_work_date:
        return build_employee_redirect(employee_key, "Укажите фактическую дату", "error", admin_mode=is_admin_mode)
    try:
        datetime.strptime(actual_work_date, "%Y-%m-%d")
    except ValueError:
        return build_employee_redirect(employee_key, "Некорректная фактическая дата", "error", admin_mode=is_admin_mode)

    if not validate_time_range(actual_work_time):
        return build_employee_redirect(
            employee_key,
            "Некорректный формат времени (ожидается HH:MM - HH:MM)",
            "error",
            admin_mode=is_admin_mode,
        )

    with get_db_connection() as conn:
        identity = get_request_identity(conn, employee_key, response_id)
        if not identity:
            return build_employee_redirect(employee_key, "Заявка не найдена или недоступна", "error", admin_mode=is_admin_mode)
        request_uid, full_name_key = identity
        if not is_admin_mode and get_period_lock_for_date(conn, "actual", actual_work_date):
            return build_employee_redirect(
                employee_key,
                "Ввод фактически отработанного времени за этот период закрыт администратором.",
                "error",
                admin_mode=False,
            )

        upsert_request_state(
            conn,
            request_uid=request_uid,
            response_id=response_id,
            full_name_key=full_name_key,
            updates={
                "status": "completed",
                "actual_work_date": actual_work_date,
                "actual_work_time": actual_work_time.strip(),
            },
        )
        conn.commit()

    return build_employee_redirect(employee_key, "Фактическое время сохранено", "success", admin_mode=is_admin_mode)


@app.post("/employee/request/create")
def employee_create_request(
    request: Request,
    employee_key: str = Form(""),
    planned_work_date: str = Form(...),
    planned_work_time: str = Form(...),
    payment_type: str = Form(""),
    task_description: str = Form(""),
    justification: str = Form(""),
    systems: str = Form(""),
    admin_mode: int = Form(0),
) -> RedirectResponse:
    is_admin_mode = bool(admin_mode) and is_admin_or_superuser_request(request)
    authenticated_employee_key = get_authenticated_employee_key(request)
    employee_key = employee_key.strip() or (authenticated_employee_key or "")

    if not employee_key:
        return redirect_with_message("/employee", "Не удалось определить сотрудника для создания заявки", "error")
    if not is_admin_mode and authenticated_employee_key != employee_key:
        return build_employee_redirect(employee_key, "Требуется повторный вход по токену", "error", admin_mode=False)

    try:
        datetime.strptime(planned_work_date, "%Y-%m-%d")
    except ValueError:
        return build_employee_redirect(employee_key, "Некорректная плановая дата", "error", admin_mode=is_admin_mode)

    if not validate_time_range(planned_work_time):
        return build_employee_redirect(
            employee_key,
            "Некорректный формат планового времени",
            "error",
            admin_mode=is_admin_mode,
        )

    normalized_payment = payment_type.strip() or "Отгул"
    normalized_systems = split_systems(systems)

    with get_db_connection() as conn:
        ensure_app_tables(conn)
        if not is_admin_mode:
            lock_info = get_lock_info_for_date(conn, planned_work_date)
            period_lock = get_period_lock_for_date(conn, "planning", planned_work_date)
            if lock_info or period_lock:
                return build_employee_redirect(
                    employee_key,
                    "Прием заявок за этот период закрыт администратором. Создание заявки доступно только администратору.",
                    "error",
                    admin_mode=False,
                )

        full_name = get_employee_display_name(conn, employee_key)
        if not full_name:
            return build_employee_redirect(employee_key, "Сотрудник не найден", "error", admin_mode=is_admin_mode)
        employee_profile = get_employee_profile(conn, employee_key)
        if not is_employee_profile_active(employee_profile):
            return build_employee_redirect(employee_key, "Профиль сотрудника неактивен", "error", admin_mode=is_admin_mode)
        if employee_profile["grade_12_plus"] and normalized_payment == "Двойная оплата":
            return build_employee_redirect(
                employee_key,
                "Двойная оплата недоступна для сотрудников с грейдом 12+",
                "error",
                admin_mode=is_admin_mode,
            )

        response_id = get_next_response_id(conn)
        now = datetime.now().isoformat(timespec="seconds")
        system_columns = [None] * 6
        for index, system_name in enumerate(normalized_systems[:6]):
            system_columns[index] = system_name

        conn.execute(
            """
            INSERT INTO survey_responses (
                response_id,
                source_row,
                start_time,
                full_name,
                full_name_normalized,
                full_name_key,
                request_type,
                grade_12_plus,
                payment_type,
                task_description,
                justification,
                planned_work_date,
                planned_work_time,
                target_work_date,
                need_additional_system_1,
                need_additional_system_2,
                need_additional_system_3,
                need_additional_system_4,
                need_additional_system_5,
                system_1,
                system_2,
                system_3,
                system_4,
                system_5,
                system_6
            ) VALUES (?, ?, ?, ?, ?, ?, 'Подать заявку', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                response_id,
                response_id,
                now,
                full_name,
                full_name,
                employee_key,
                int(employee_profile["grade_12_plus"]),
                normalized_payment,
                task_description.strip(),
                justification.strip(),
                planned_work_date,
                planned_work_time.strip(),
                planned_work_date,
                1 if len(normalized_systems) > 1 else 0,
                1 if len(normalized_systems) > 2 else 0,
                1 if len(normalized_systems) > 3 else 0,
                1 if len(normalized_systems) > 4 else 0,
                1 if len(normalized_systems) > 5 else 0,
                *system_columns,
            ),
        )
        conn.execute("DELETE FROM response_systems WHERE response_id = ?", (response_id,))
        for system_order, system_name in enumerate(normalized_systems, start=1):
            conn.execute(
                "INSERT INTO response_systems (response_id, system_order, system_name) VALUES (?, ?, ?)",
                (response_id, system_order, system_name),
            )
        conn.commit()

    return build_employee_redirect(employee_key, "Новая заявка создана", "success", admin_mode=is_admin_mode)


@app.post("/employee/request/cancel")
def employee_cancel_request(
    request: Request,
    employee_key: str = Form(...),
    response_id: int = Form(...),
    admin_mode: int = Form(0),
) -> RedirectResponse:
    is_admin_mode = bool(admin_mode) and is_admin_or_superuser_request(request)
    authenticated_employee_key = get_authenticated_employee_key(request)
    if not is_admin_mode and authenticated_employee_key != employee_key:
        return build_employee_redirect(employee_key, "Требуется повторный вход по токену", "error", admin_mode=False)
    with get_db_connection() as conn:
        identity = get_request_identity(conn, employee_key, response_id)
        if not identity:
            return build_employee_redirect(employee_key, "Заявка не найдена или недоступна", "error", admin_mode=is_admin_mode)
        request_uid, full_name_key = identity
        row = conn.execute(
            "SELECT planned_work_date FROM survey_responses WHERE response_id = ?;",
            (response_id,),
        ).fetchone()
        planned_date = row["planned_work_date"] if row else None
        if not is_admin_mode and planned_date and (
            get_lock_info(conn, response_id) or get_period_lock_for_date(conn, "planning", planned_date)
        ):
            return build_employee_redirect(
                employee_key,
                "Прием заявок за этот период закрыт администратором. Отмена доступна только администратору.",
                "error",
                admin_mode=False,
            )

        upsert_request_state(
            conn,
            request_uid=request_uid,
            response_id=response_id,
            full_name_key=full_name_key,
            updates={"status": "cancelled"},
        )
        conn.commit()

    return build_employee_redirect(employee_key, "Заявка переведена в статус: Отменена", "success", admin_mode=is_admin_mode)


@app.post("/employee/request/correct")
def employee_correct_request(
    request: Request,
    employee_key: str = Form(...),
    response_id: int = Form(...),
    planned_work_date: str = Form(...),
    planned_work_time: str = Form(...),
    payment_type: str = Form(""),
    task_description: str = Form(""),
    justification: str = Form(""),
    systems: str = Form(""),
    admin_mode: int = Form(0),
) -> RedirectResponse:
    is_admin_mode = bool(admin_mode) and is_admin_or_superuser_request(request)
    authenticated_employee_key = get_authenticated_employee_key(request)
    if not is_admin_mode and authenticated_employee_key != employee_key:
        return build_employee_redirect(employee_key, "Требуется повторный вход по токену", "error", admin_mode=False)
    if planned_work_date:
        try:
            datetime.strptime(planned_work_date, "%Y-%m-%d")
        except ValueError:
            return build_employee_redirect(employee_key, "Некорректная плановая дата", "error", admin_mode=is_admin_mode)

    if planned_work_time and not validate_time_range(planned_work_time):
        return build_employee_redirect(
            employee_key,
            "Некорректный формат планового времени",
            "error",
            admin_mode=is_admin_mode,
        )

    with get_db_connection() as conn:
        identity = get_request_identity(conn, employee_key, response_id)
        if not identity:
            return build_employee_redirect(employee_key, "Заявка не найдена или недоступна", "error", admin_mode=is_admin_mode)
        request_uid, full_name_key = identity
        effective_date = planned_work_date.strip()
        if not is_admin_mode and effective_date and (
            get_lock_info(conn, response_id) or get_period_lock_for_date(conn, "planning", effective_date)
        ):
            return build_employee_redirect(
                employee_key,
                "Прием заявок за этот период закрыт администратором. Корректировка доступна только администратору.",
                "error",
                admin_mode=False,
            )
        employee_profile = get_employee_profile(conn, full_name_key)
        if employee_profile["grade_12_plus"] and payment_type.strip() == "Двойная оплата":
            return build_employee_redirect(
                employee_key,
                "Двойная оплата недоступна для сотрудников с грейдом 12+",
                "error",
                admin_mode=is_admin_mode,
            )

        existing = get_request_state(conn, request_uid)
        current_status = existing.get("status") if existing else "active"
        if current_status not in VALID_STATUSES:
            current_status = "active"

        upsert_request_state(
            conn,
            request_uid=request_uid,
            response_id=response_id,
            full_name_key=full_name_key,
            updates={
                "status": current_status,
                "is_corrected": 1,
                "corrected_at": datetime.now().isoformat(timespec="seconds"),
                "override_planned_work_date": planned_work_date.strip() or None,
                "override_planned_work_time": planned_work_time.strip() or None,
                "override_payment_type": payment_type.strip() or None,
                "override_task_description": task_description.strip() or None,
                "override_justification": justification.strip() or None,
                "override_systems": normalize_systems_text(systems),
            },
        )
        conn.commit()

    return build_employee_redirect(employee_key, "Заявка откорректирована", "success", admin_mode=is_admin_mode)


@app.post("/admin/locks/create")
def admin_create_period_lock(
    request: Request,
    lock_type: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    comment: str = Form(""),
) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return redirect_with_message("/", "Закрытие периода доступно только администратору", "error")
    with get_db_connection() as conn:
        ensure_app_tables(conn)
        try:
            create_period_lock(
                conn,
                lock_type=lock_type,
                date_from=date_from,
                date_to=date_to,
                created_by=get_admin_actor_key(request),
                comment=comment,
            )
        except ValueError as exc:
            return redirect_with_message("/admin", str(exc), "error")
        conn.commit()
    label = "приема заявок" if lock_type == "planning" else "ввода факта"
    return redirect_with_message("/admin", f"Период {label} закрыт", "success")


@app.post("/upload")
async def upload_and_ingest(request: Request, file: UploadFile = File(...)) -> RedirectResponse:
    del request, file
    return RedirectResponse(
        url="/?msg=Загрузка выгрузок через Web UI отключена. Работа ведется через заявки в системе.&level=info",
        status_code=303,
    )


@app.post("/generate/full")
def generate_full_report(request: Request, date_from: str = Form(...), date_to: str = Form(...)) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return RedirectResponse(url="/?msg=Формирование отчетов доступно только администратору&level=error", status_code=303)
    try:
        parsed_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        parsed_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        return redirect_with_message("/admin", "Некорректный диапазон недели", "error")

    if parsed_from > parsed_to:
        return redirect_with_message("/admin", "Дата начала недели позже даты окончания", "error")

    output_name = f"Отчеты выхода выходные {date_from}_{date_to}.xlsx"
    output_path = REPORTS_DIR / output_name

    try:
        run_script(
            "build_weekend_reports.py",
            [
                "--db",
                str(DB_PATH),
                "--employees-csv",
                "",
                "--date-from",
                date_from,
                "--date-to",
                date_to,
                "--output",
                str(output_path),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return redirect_with_message("/admin", f"Ошибка генерации общего отчета: {exc}", "error")

    return redirect_with_message("/admin", f"Сформирован {output_name}", "success")


@app.post("/generate/actual")
def generate_actual_report(request: Request, date_from: str = Form(...), date_to: str = Form(...)) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return RedirectResponse(url="/?msg=Формирование отчетов доступно только администратору&level=error", status_code=303)
    output_name = f"management_report_3_actual_{date_from}_{date_to}.xlsx"
    output_path = REPORTS_DIR / output_name

    try:
        run_script(
            "report_third_closure.py",
            [
                "--db",
                str(DB_PATH),
                "--date-from",
                date_from,
                "--date-to",
                date_to,
                "--output",
                str(output_path),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return redirect_with_message("/admin", f"Ошибка генерации отчета 3: {exc}", "error")

    return redirect_with_message("/admin", f"Сформирован {output_name}", "success")


@app.post("/generate/reconciliation")
def generate_reconciliation_report(request: Request, date_from: str = Form(...), date_to: str = Form(...)) -> RedirectResponse:
    if not is_admin_or_superuser_request(request):
        return RedirectResponse(url="/?msg=Формирование отчетов доступно только администратору&level=error", status_code=303)
    output_name = f"management_report_4_reconciliation_{date_from}_{date_to}.xlsx"
    output_path = REPORTS_DIR / output_name

    try:
        run_script(
            "report_four_reconciliation.py",
            [
                "--db",
                str(DB_PATH),
                "--date-from",
                date_from,
                "--date-to",
                date_to,
                "--output",
                str(output_path),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return redirect_with_message("/admin", f"Ошибка генерации сверки: {exc}", "error")

    return redirect_with_message("/admin", f"Сформирован {output_name}", "success")


@app.get("/download/{filename}")
def download_report(request: Request, filename: str) -> FileResponse:
    if not is_admin_or_superuser_request(request):
        raise HTTPException(status_code=403, detail="Скачивание отчетов доступно только администратору")
    target = REPORTS_DIR / Path(filename).name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path=target, filename=target.name)
