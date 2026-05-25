from __future__ import annotations

import sqlite3

VALID_STATUSES = {"active", "cancelled", "completed"}
STATUS_LABELS = {
    "active": "Активна",
    "cancelled": "Отменена",
    "completed": "Фактическое время указано",
}


def ensure_app_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_employee_directory (
            full_name_key TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            work_email TEXT,
            local_phone TEXT,
            mobile_phone TEXT,
            position_short_name TEXT,
            grade_num INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_request_state (
            request_uid TEXT PRIMARY KEY,
            response_id INTEGER NOT NULL,
            full_name_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'cancelled', 'completed')),
            is_corrected INTEGER NOT NULL DEFAULT 0 CHECK(is_corrected IN (0, 1)),
            override_planned_work_date TEXT,
            override_planned_work_time TEXT,
            override_payment_type TEXT,
            override_task_description TEXT,
            override_justification TEXT,
            override_systems TEXT,
            actual_work_date TEXT,
            actual_work_time TEXT,
            corrected_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_request_state_full_name_key
        ON app_request_state (full_name_key);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_report_lock (
            response_id INTEGER PRIMARY KEY,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            report_file TEXT NOT NULL,
            locked_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_report_lock_week
        ON app_report_lock (week_start, week_end);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_employee_auth (
            full_name_key TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            token_issued_at TEXT NOT NULL,
            token_reissued_at TEXT,
            forgot_requested_at TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(app_employee_auth)").fetchall()
    }
    if "forgot_requested_at" not in existing_columns:
        conn.execute("ALTER TABLE app_employee_auth ADD COLUMN forgot_requested_at TEXT;")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_employee_profile (
            full_name_key TEXT PRIMARY KEY,
            grade_12_plus INTEGER NOT NULL DEFAULT 0 CHECK(grade_12_plus IN (0, 1)),
            updated_at TEXT NOT NULL
        );
        """
    )
    profile_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(app_employee_profile)").fetchall()
    }
    profile_column_defs = {
        "is_admin": "INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0, 1))",
        "is_superuser": "INTEGER NOT NULL DEFAULT 0 CHECK(is_superuser IN (0, 1))",
        "employee_status": "TEXT NOT NULL DEFAULT 'active' CHECK(employee_status IN ('active', 'blocked', 'archived'))",
        "status_reason": "TEXT",
        "blocked_at": "TEXT",
        "archived_at": "TEXT",
        "restored_at": "TEXT",
        "updated_by": "TEXT",
    }
    for column_name, column_def in profile_column_defs.items():
        if column_name not in profile_columns:
            conn.execute(f"ALTER TABLE app_employee_profile ADD COLUMN {column_name} {column_def};")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_period_lock (
            lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
            lock_type TEXT NOT NULL CHECK(lock_type IN ('planning', 'actual')),
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            comment TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_period_lock_type_dates
        ON app_period_lock (lock_type, date_from, date_to);
        """
    )
