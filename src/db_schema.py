from __future__ import annotations

import sqlite3


def ensure_core_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS survey_responses (
            response_id INTEGER PRIMARY KEY,
            source_row INTEGER,
            source_id REAL,
            start_time TEXT,
            duration_raw TEXT,
            duration_seconds REAL,
            duration_minutes REAL,
            channel TEXT,
            status TEXT,
            comment TEXT,
            agreement REAL,
            full_name TEXT,
            full_name_normalized TEXT,
            full_name_key TEXT,
            request_type TEXT,
            grade_12_plus REAL,
            payment_type TEXT,
            task_description TEXT,
            justification TEXT,
            planned_work_date TEXT,
            planned_work_time TEXT,
            approver TEXT,
            actual_work_date TEXT,
            actual_work_time TEXT,
            target_work_date TEXT,
            logical_key TEXT,
            need_additional_system_1 REAL,
            need_additional_system_2 REAL,
            need_additional_system_3 REAL,
            need_additional_system_4 REAL,
            need_additional_system_5 REAL,
            system_1 TEXT,
            system_2 TEXT,
            system_3 TEXT,
            system_4 TEXT,
            system_5 TEXT,
            system_6 TEXT,
            row_hash TEXT,
            source_file TEXT,
            source_file_hash TEXT,
            loaded_at TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_survey_responses_full_name_key
        ON survey_responses (full_name_key);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_survey_responses_request_type_dates
        ON survey_responses (request_type, planned_work_date, actual_work_date);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS response_systems (
            response_id INTEGER NOT NULL,
            system_order INTEGER NOT NULL,
            system_name TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_response_systems_response_id
        ON response_systems (response_id);
        """
    )
