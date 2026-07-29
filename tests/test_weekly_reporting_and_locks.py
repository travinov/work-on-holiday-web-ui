from __future__ import annotations

import sqlite3
import os
import importlib
import sys
import tempfile
import unittest
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.app_request_state import ensure_app_tables
from src import generate_users, init_db, web_ui, work_time


SUPERUSER_ENV = {
    "WORK_ON_HOLIDAY_SUPERUSER_LOGIN": "root",
    "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD": "release-password",
}
SECURE_SUPERUSER_ENV = {
    **SUPERUSER_ENV,
    "WORK_ON_HOLIDAY_SECURE_COOKIES": "1",
}


def login_superuser(client: TestClient):
    return client.post(
        "/superuser/login",
        data={"superuser_login": "root", "superuser_password": "release-password"},
        follow_redirects=False,
    )


SURVEY_SCHEMA = """
CREATE TABLE survey_responses (
    response_id INTEGER,
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

SYSTEMS_SCHEMA = """
CREATE TABLE response_systems (
    response_id INTEGER,
    system_order INTEGER,
    system_name TEXT
);
"""


def init_test_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(SURVEY_SCHEMA)
        conn.execute(SYSTEMS_SCHEMA)
        ensure_app_tables(conn)
        conn.commit()


def insert_planned_request(db_path: Path, *, response_id: int, full_name: str, full_name_key: str, planned_date: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO survey_responses (
                response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                request_type, payment_type, task_description, justification,
                planned_work_date, planned_work_time, target_work_date
            ) VALUES (?, ?, ?, ?, ?, ?, 'Подать заявку', 'Отгул', 'Задача', 'Причина', ?, '09:00 - 18:00', ?)
            """,
            (
                response_id,
                response_id,
                f"2026-04-20T0{response_id}:00:00",
                full_name,
                full_name,
                full_name_key,
                planned_date,
                planned_date,
            ),
        )
        conn.execute(
            "INSERT INTO response_systems (response_id, system_order, system_name) VALUES (?, 1, 'Система A')",
            (response_id,),
        )
        conn.commit()


def future_date_iso(days_ahead: int = 30) -> str:
    return (web_ui.date.today() + web_ui.timedelta(days=days_ahead)).isoformat()


def future_week_start(days_ahead: int = 30) -> web_ui.date:
    anchor = web_ui.date.today() + web_ui.timedelta(days=days_ahead)
    candidate = anchor + web_ui.timedelta(days=(7 - anchor.weekday()) % 7)
    if candidate <= web_ui.date.today():
        candidate += web_ui.timedelta(days=7)
    return candidate


def insert_legacy_employee(db_path: Path, *, full_name: str, token: str) -> str:
    full_name_key = web_ui.normalize_name_key(full_name)
    assert full_name_key is not None
    now = web_ui.datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_employee_directory (
                full_name_key, full_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (full_name_key, full_name, now, now),
        )
        conn.execute(
            """
            INSERT INTO app_employee_profile (full_name_key, grade_12_plus, updated_at)
            VALUES (?, 0, ?)
            """,
            (full_name_key, now),
        )
        conn.execute(
            """
            INSERT INTO app_employee_auth (
                full_name_key, token_hash, token_issued_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (full_name_key, web_ui.hash_employee_token(token), now, now),
        )
        conn.commit()
    return full_name_key


def xlsx_shared_strings(zip_file: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []
    root = ET.fromstring(zip_file.read("xl/sharedStrings.xml"))
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared_strings: list[str] = []
    for item in root.findall("main:si", namespace):
        shared_strings.append("".join(node.text or "" for node in item.findall(".//main:t", namespace)))
    return shared_strings


def xlsx_sheet_path(zip_file: zipfile.ZipFile, sheet_name: str) -> str:
    workbook_ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rels_ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    workbook_root = ET.fromstring(zip_file.read("xl/workbook.xml"))
    rels_root = ET.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
    target_id = None
    for sheet in workbook_root.findall("main:sheets/main:sheet", workbook_ns):
        if sheet.attrib.get("name") == sheet_name:
            target_id = sheet.attrib.get(f"{{{workbook_ns['r']}}}id")
            break
    assert target_id is not None
    target_path = None
    for rel in rels_root.findall("rel:Relationship", rels_ns):
        if rel.attrib.get("Id") == target_id:
            target_path = rel.attrib.get("Target")
            break
    assert target_path is not None
    target_path = target_path.lstrip("/")
    if target_path.startswith("xl/"):
        return target_path
    return f"xl/{target_path}"


def xlsx_sheet_cells(zip_file: zipfile.ZipFile, sheet_name: str) -> dict[str, str]:
    shared_strings = xlsx_shared_strings(zip_file)
    sheet_root = ET.fromstring(zip_file.read(xlsx_sheet_path(zip_file, sheet_name)))
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cells: dict[str, str] = {}
    for cell in sheet_root.findall("main:sheetData/main:row/main:c", namespace):
        ref = cell.attrib["r"]
        cell_type = cell.attrib.get("t")
        value_node = cell.find("main:v", namespace)
        if cell_type == "s" and value_node is not None:
            cells[ref] = shared_strings[int(value_node.text or "0")]
        elif cell_type == "inlineStr":
            text_nodes = cell.findall(".//main:t", namespace)
            cells[ref] = "".join(node.text or "" for node in text_nodes)
        elif value_node is not None:
            cells[ref] = value_node.text or ""
        else:
            cells[ref] = ""
    return cells


def xlsx_sheet_rows(zip_file: zipfile.ZipFile, sheet_name: str) -> dict[int, dict[str, str]]:
    shared_strings = xlsx_shared_strings(zip_file)
    sheet_root = ET.fromstring(zip_file.read(xlsx_sheet_path(zip_file, sheet_name)))
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: dict[int, dict[str, str]] = {}
    for row in sheet_root.findall("main:sheetData/main:row", namespace):
        row_index = int(row.attrib["r"])
        row_cells: dict[str, str] = {}
        for cell in row.findall("main:c", namespace):
            ref = cell.attrib["r"]
            column = "".join(char for char in ref if char.isalpha())
            cell_type = cell.attrib.get("t")
            value_node = cell.find("main:v", namespace)
            if cell_type == "s" and value_node is not None:
                row_cells[column] = shared_strings[int(value_node.text or "0")]
            elif cell_type == "inlineStr":
                text_nodes = cell.findall(".//main:t", namespace)
                row_cells[column] = "".join(node.text or "" for node in text_nodes)
            elif value_node is not None:
                row_cells[column] = value_node.text or ""
            else:
                row_cells[column] = ""
        rows[row_index] = row_cells
    return rows


class WeeklyReportingAndLocksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite3"
        init_test_db(self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_xlsx_sheet_path_normalizes_relationship_targets_without_double_prefix(self) -> None:
        workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""
        rel_template = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="{target}"/>
</Relationships>
"""

        for target in ("/xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.xml", "worksheets/sheet1.xml"):
            archive_path = Path(self.tmpdir.name) / f"{target.replace('/', '_')}.xlsx"
            with zipfile.ZipFile(archive_path, "w") as zip_file:
                zip_file.writestr("xl/workbook.xml", workbook_xml)
                zip_file.writestr("xl/_rels/workbook.xml.rels", rel_template.format(target=target))

            with zipfile.ZipFile(archive_path) as zip_file:
                self.assertEqual("xl/worksheets/sheet1.xml", xlsx_sheet_path(zip_file, "Sheet1"))

    def test_init_db_creates_web_only_schema_without_ingestion_table(self) -> None:
        initialized_db = Path(self.tmpdir.name) / "initialized.sqlite3"

        init_db.initialize_database(initialized_db)

        with sqlite3.connect(initialized_db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        self.assertIn("survey_responses", tables)
        self.assertIn("response_systems", tables)
        self.assertIn("app_employee_directory", tables)
        self.assertIn("app_employee_profile", tables)
        self.assertNotIn("ingestion_files", tables)

    def test_generated_users_can_login_without_seed_requests(self) -> None:
        initialized_db = Path(self.tmpdir.name) / "generated_users.sqlite3"
        init_db.initialize_database(initialized_db)

        created_users = generate_users.generate_users(
            initialized_db,
            count=3,
            seed=11,
            overwrite=True,
        )

        self.assertEqual(3, len(created_users))
        first_user = created_users[0]

        with patch.object(web_ui, "DB_PATH", initialized_db):
            client = TestClient(web_ui.app)
            login_response = client.post(
                "/employee/login",
                data={"full_name": first_user.full_name},
            )

        self.assertEqual(200, login_response.status_code)
        self.assertIn("Токен сотрудника создан", login_response.text)

        with sqlite3.connect(initialized_db) as conn:
            requests_count = conn.execute("SELECT COUNT(*) FROM survey_responses").fetchone()[0]
            profile = conn.execute(
                "SELECT grade_12_plus FROM app_employee_profile WHERE full_name_key = ?",
                (first_user.full_name_key,),
            ).fetchone()

        self.assertEqual(0, requests_count)
        self.assertIsNotNone(profile)
        self.assertEqual(1 if first_user.grade_12_plus else 0, profile[0])

    def test_work_time_validation_is_strict_and_splits_overnight_ranges(self) -> None:
        self.assertFalse(work_time.validate_time_range("00:00 - 00:00"))
        self.assertTrue(work_time.validate_time_range("23:00 - 00:00"))

        short_range = work_time.parse_work_time("00:00 - 04:59")
        five_hour_range = work_time.parse_work_time("00:00 - 05:00")
        five_hour_one_range = work_time.parse_work_time("00:00 - 05:01")
        overnight_segments = work_time.split_overnight_interval(web_ui.date(2026, 4, 23), "23:00 - 04:00")

        self.assertEqual(299, short_range.duration_minutes)
        self.assertFalse(short_range.lunch_warning)
        self.assertTrue(five_hour_range.lunch_warning)
        self.assertTrue(five_hour_one_range.lunch_warning)
        self.assertEqual(
            [
                (web_ui.date(2026, 4, 23), "23:00 - 00:00"),
                (web_ui.date(2026, 4, 24), "00:00 - 04:00"),
            ],
            [(segment.work_date, segment.time_range) for segment in overnight_segments],
        )
        self.assertFalse(
            any(work_time.parse_work_time(segment.time_range).lunch_warning for segment in overnight_segments)
        )

        invalid_time_cases = [
            ("33:00 - 34:00", "Некорректное время"),
            ("24:00 - 25:00", "Некорректное время"),
            ("10:60 - 11:00", "Некорректное время"),
            ("10:00 - 10:00", "Продолжительность рабочего интервала должна быть больше нуля"),
            ("10:00 -", "Некорректный формат времени"),
            ("10:00", "Некорректный формат времени"),
        ]
        for value, expected_message in invalid_time_cases:
            with self.subTest(value=value):
                self.assertFalse(work_time.validate_time_range(value))
                with self.assertRaisesRegex(ValueError, expected_message):
                    work_time.parse_work_time(value)

    def test_iso_date_validation_is_strict_for_partial_and_nonexistent_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "Некорректная дата, ожидается формат ГГГГ-ММ-ДД"):
            web_ui.parse_iso_date("2026-05")
        with self.assertRaisesRegex(ValueError, "Некорректная календарная дата"):
            web_ui.parse_iso_date("2026-02-30")

        self.assertEqual(web_ui.date(2026, 5, 23), web_ui.parse_iso_date("2026-05-23"))

    def test_midnight_boundary_actual_time_is_supported_but_true_overnight_is_rejected(self) -> None:
        boundary_range = work_time.parse_work_time("23:00 - 00:00")

        self.assertEqual(60, boundary_range.duration_minutes)
        self.assertFalse(boundary_range.is_overnight)
        self.assertEqual("23:00 - 00:00", boundary_range.normalized)

        insert_planned_request(
            self.db_path,
            response_id=4101,
            full_name="Границев Григорий Иванович",
            full_name_key="границев григорий иванович",
            planned_date="2026-08-03",
        )
        insert_planned_request(
            self.db_path,
            response_id=4102,
            full_name="Границев Григорий Иванович",
            full_name_key="границев григорий иванович",
            planned_date="2026-08-03",
        )
        insert_planned_request(
            self.db_path,
            response_id=4103,
            full_name="Границев Григорий Иванович",
            full_name_key="границев григорий иванович",
            planned_date="2026-08-03",
        )
        with sqlite3.connect(self.db_path) as conn:
            now = "2026-07-23T10:00:00"
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:4101', 4101, 'границев григорий иванович', 'in_progress', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:4102', 4102, 'границев григорий иванович', 'in_progress', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:4103', 4103, 'границев григорий иванович', 'in_progress', ?, ?)
                """,
                (now, now),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Границев Григорий Иванович"}).status_code)
            saved = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "границев григорий иванович",
                    "response_id": "4101",
                    "actual_work_date": "2026-08-03",
                    "actual_work_time": "23:00 - 00:00",
                },
                follow_redirects=False,
            )
            rejected = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "границев григорий иванович",
                    "response_id": "4102",
                    "actual_work_date": "2026-08-03",
                    "actual_work_time": "23:00 - 05:00",
                },
                follow_redirects=False,
            )
            malformed = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "границев григорий иванович",
                    "response_id": "4103",
                    "actual_work_date": "2026-08-03",
                    "actual_work_time": "10:60 - 11:00",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, saved.status_code)
        self.assertIn("фактическое время сохранено", unquote(saved.headers["location"]).lower())
        self.assertEqual(303, rejected.status_code)
        self.assertIn("через полночь", unquote(rejected.headers["location"]).lower())
        self.assertEqual(303, malformed.status_code)
        self.assertIn("некорректное время", unquote(malformed.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            saved_row = conn.execute(
                "SELECT actual_work_date, actual_work_time FROM app_request_state WHERE response_id = 4101"
            ).fetchone()
            rejected_row = conn.execute(
                "SELECT actual_work_date, actual_work_time FROM app_request_state WHERE response_id = 4102"
            ).fetchone()
            malformed_row = conn.execute(
                "SELECT actual_work_date, actual_work_time FROM app_request_state WHERE response_id = 4103"
            ).fetchone()
        self.assertEqual(("2026-08-03", "23:00 - 00:00"), saved_row)
        self.assertEqual((None, None), rejected_row)
        self.assertEqual((None, None), malformed_row)

    def test_ensure_app_tables_migrates_old_request_status_constraint(self) -> None:
        legacy_db = Path(self.tmpdir.name) / "legacy_status.sqlite3"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute(
                """
                CREATE TABLE app_request_state (
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
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:1', 1, 'legacy user', 'active', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            ensure_app_tables(conn)
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:2', 2, 'legacy user', 'in_progress', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            statuses = conn.execute(
                "SELECT request_uid, status, returned_for_correction FROM app_request_state ORDER BY request_uid"
            ).fetchall()

        self.assertEqual([("req:1", "active", 0), ("req:2", "in_progress", 0)], statuses)

    def test_report_week_filter_uses_corrected_planned_date(self) -> None:
        report_second_requests = importlib.import_module("src.report_second_requests")
        insert_planned_request(
            self.db_path,
            response_id=101,
            full_name="Иванов Иван Иванович",
            full_name_key="иванов иван иванович",
            planned_date="2026-04-30",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, is_corrected,
                    override_planned_work_date, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', 1, ?, '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """,
                ("req:101", 101, "иванов иван иванович", "2026-04-22"),
            )
            conn.commit()

        report_df = report_second_requests.build_report_dataframe(
            str(self.db_path),
            date_from=web_ui.date(2026, 4, 21),
            date_to=web_ui.date(2026, 4, 27),
        )

        self.assertEqual(1, len(report_df))
        self.assertEqual("Иванов Иван Иванович", report_df.iloc[0]["ФИО"])
        self.assertEqual("22.04.2026", report_df.iloc[0]["Плановая дата выхода"])

    def test_planned_reports_include_requests_after_actual_time_is_entered(self) -> None:
        report_first_management = importlib.import_module("src.report_first_management")
        report_second_requests = importlib.import_module("src.report_second_requests")
        insert_planned_request(
            self.db_path,
            response_id=102,
            full_name="Фактов Федор Иванович",
            full_name_key="фактов федор иванович",
            planned_date="2026-04-25",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date,
                    actual_work_time, created_at, updated_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, '2026-04-25T14:00:00', '2026-04-25T14:00:00')
                """,
                ("req:102", 102, "фактов федор иванович", "2026-04-25", "09:00 - 18:00"),
            )
            conn.commit()

        date_from = web_ui.date(2026, 4, 20)
        date_to = web_ui.date(2026, 4, 26)
        report_1_df = report_first_management.build_report_dataframe(str(self.db_path), date_from, date_to)
        report_2_df = report_second_requests.build_report_dataframe(str(self.db_path), date_from, date_to)

        self.assertEqual(1, len(report_1_df))
        self.assertEqual(1, len(report_2_df))
        self.assertEqual("Фактов Федор Иванович", report_1_df.iloc[0]["ФИО"])
        self.assertEqual("Фактов Федор Иванович", report_2_df.iloc[0]["ФИО"])

    def test_new_employee_login_accepts_cyrillic_yo_and_hyphen_and_rejects_invalid_names_but_legacy_login_still_works(self) -> None:
        valid_name = "Ёлкин Иван-Петрович Сергеевич"
        legacy_name = "Легаси Имя-Ёжевич"
        legacy_token = "legacy-token-1"
        insert_legacy_employee(self.db_path, full_name=legacy_name, token=legacy_token)

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            valid_login = client.post("/employee/login", data={"full_name": valid_name}, follow_redirects=False)
            invalid_two_parts = client.post(
                "/employee/login",
                data={"full_name": "Иванов Иван"},
                follow_redirects=False,
            )
            invalid_latin = client.post(
                "/employee/login",
                data={"full_name": "John Doe Smith"},
                follow_redirects=False,
            )
            invalid_digits = client.post(
                "/employee/login",
                data={"full_name": "Иванов 1ван Иванович"},
                follow_redirects=False,
            )

        self.assertEqual(200, valid_login.status_code)
        self.assertIn("Токен сотрудника создан", valid_login.text)
        for response in (invalid_two_parts, invalid_latin, invalid_digits):
            self.assertEqual(303, response.status_code)
            self.assertIn("новой регистрации", unquote(response.headers["location"]).lower())

        with patch.object(web_ui, "DB_PATH", self.db_path):
            legacy_client = TestClient(web_ui.app)
            legacy_login = legacy_client.post(
                "/employee/login",
                data={"full_name": legacy_name, "access_token": legacy_token},
                follow_redirects=False,
            )

        self.assertEqual(303, legacy_login.status_code)
        self.assertIn("вход выполнен", unquote(legacy_login.headers["location"]).lower())

    def test_actual_report_accepts_week_range_with_state_overrides(self) -> None:
        report_third_closure = importlib.import_module("src.report_third_closure")
        insert_planned_request(
            self.db_path,
            response_id=303,
            full_name="Сидоров Сидор Сидорович",
            full_name_key="сидоров сидор сидорович",
            planned_date="2026-07-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date,
                    actual_work_time, created_at, updated_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, '2026-04-22T21:00:00', '2026-04-22T21:00:00')
                """,
                ("req:303", 303, "сидоров сидор сидорович", "2026-04-23", "00:00 - 04:00"),
            )
            conn.commit()

        report_df = report_third_closure.build_report_dataframe(
            str(self.db_path),
            date_from=web_ui.date(2026, 4, 20),
            date_to=web_ui.date(2026, 4, 26),
        )

        self.assertEqual(1, len(report_df))
        self.assertEqual("Сидоров Сидор Сидорович", report_df.iloc[0]["ФИО"])
        self.assertEqual("23.04.2026", report_df.iloc[0]["Дата фактического выхода"])

    def test_locked_week_blocks_employee_correction_without_admin_auth(self) -> None:
        correction_date = future_date_iso(44)
        insert_planned_request(
            self.db_path,
            response_id=202,
            full_name="Петров Петр Петрович",
            full_name_key="петров петр петрович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_report_lock (response_id, week_start, week_end, report_file, locked_at)
                VALUES (202, '2026-04-20', '2026-04-26', 'week.xlsx', '2026-04-20T09:00:00')
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            first_login = client.post("/employee/login", data={"full_name": "Петров Петр Петрович"})
            self.assertEqual(200, first_login.status_code)

            blocked = client.post(
                "/employee/request/correct",
                data={
                    "employee_key": "петров петр петрович",
                    "response_id": "202",
                    "planned_work_date": correction_date,
                    "planned_work_time": "10:00 - 19:00",
                    "payment_type": "Отгул",
                    "task_description": "Новая задача",
                    "justification": "Причина",
                    "systems": "Система A",
                    "admin_mode": "1",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, blocked.status_code)
        self.assertIn("только администратору", unquote(blocked.headers["location"]).lower())

    def test_employee_can_create_new_request_and_it_appears_in_cabinet(self) -> None:
        planned_seed_date = future_date_iso(26)
        create_date = future_week_start(27)
        create_date_iso = create_date.isoformat()
        insert_planned_request(
            self.db_path,
            response_id=909,
            full_name="Новиков Павел Андреевич",
            full_name_key="новиков павел андреевич",
            planned_date=planned_seed_date,
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = client.post("/employee/login", data={"full_name": "Новиков Павел Андреевич"})
            self.assertEqual(200, login.status_code)

            create_response = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": create_date_iso,
                    "planned_work_time": "19:00 - 22:00",
                    "payment_type": "Двойная оплата",
                    "task_description": "Ночной релиз",
                    "justification": "Окно сопровождения",
                    "systems": "Система B | Система C",
                },
                follow_redirects=False,
            )

            self.assertEqual(303, create_response.status_code)
            self.assertIn("заявка создана", unquote(create_response.headers["location"]).lower())

            cabinet = client.get("/employee")

        self.assertEqual(200, cabinet.status_code)
        self.assertIn("Ночной релиз", cabinet.text)
        self.assertIn(web_ui.to_ru_date(create_date_iso), cabinet.text)
        self.assertIn(web_ui.to_ru_weekday(create_date_iso), cabinet.text.lower())
        self.assertIn("19:00 - 22:00", cabinet.text)
        self.assertIn("Система B", cabinet.text)

    def test_create_request_rejects_invalid_date_time_payment_and_required_fields_without_inserting_rows(self) -> None:
        self.assertIsNone(
            web_ui.validate_required_request_fields(
                payment_type="Отгул",
                task_description="А" * 500,
                justification="Причина",
                systems=["Система A"],
            )
        )
        planned_date = future_date_iso(31)
        insert_planned_request(
            self.db_path,
            response_id=1111,
            full_name="Обязов Олег Иванович",
            full_name_key="обязов олег иванович",
            planned_date=planned_date,
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Обязов Олег Иванович"}).status_code)
            with sqlite3.connect(self.db_path) as conn:
                initial_rows = conn.execute("SELECT COUNT(*) FROM survey_responses").fetchone()[0]

            cases = [
                ("planned_work_date_partial", "Некорректная плановая дата", {"planned_work_date": "2026-05"}),
                ("planned_work_date_nonexistent", "Некорректная плановая дата", {"planned_work_date": "2026-02-30"}),
                ("planned_work_time_33", "Некорректное время", {"planned_work_time": "33:00 - 34:00"}),
                ("planned_work_time_24", "Некорректное время", {"planned_work_time": "24:00 - 25:00"}),
                ("planned_work_time_minute_60", "Некорректное время", {"planned_work_time": "10:60 - 11:00"}),
                ("planned_work_time_zero", "Продолжительность рабочего интервала должна быть больше нуля", {"planned_work_time": "10:00 - 10:00"}),
                ("planned_work_time_partial", "Некорректный формат времени", {"planned_work_time": "10:00 -"}),
                ("payment_type_blank", "Укажите тип компенсации", {"payment_type": ""}),
                (
                    "payment_type_invalid",
                    "Некорректный тип компенсации. Допустимые значения: Отгул, Двойная оплата",
                    {"payment_type": "Бонусом"},
                ),
                ("task_description", "Укажите задачу", {"task_description": ""}),
                (
                    "task_description_too_long",
                    "Задача не может быть длиннее 500 символов",
                    {"task_description": "А" * 501},
                ),
                ("justification", "Укажите обоснование", {"justification": ""}),
                ("systems", "Укажите хотя бы одну систему", {"systems": ""}),
            ]
            for field_name, expected_message, overrides in cases:
                with self.subTest(field=field_name):
                    response = client.post(
                        "/employee/request/create",
                        data={
                            "planned_work_date": planned_date,
                            "planned_work_time": "10:00 - 12:00",
                            "payment_type": "Отгул",
                            "task_description": "Задача",
                            "justification": "Причина",
                            "systems": "Система A",
                            **overrides,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(303, response.status_code)
                    self.assertIn(expected_message.lower(), unquote(response.headers["location"]).lower())
                    with sqlite3.connect(self.db_path) as conn:
                        rows_after = conn.execute("SELECT COUNT(*) FROM survey_responses").fetchone()[0]
                    self.assertEqual(initial_rows, rows_after)

    def test_create_request_splits_overnight_request_and_rejects_locked_second_segment(self) -> None:
        planned_date = future_date_iso(32)
        locked_date = (web_ui.date.fromisoformat(planned_date) + web_ui.timedelta(days=1)).isoformat()
        insert_planned_request(
            self.db_path,
            response_id=1112,
            full_name="Ночной Никита Иванович",
            full_name_key="ночной никита иванович",
            planned_date=planned_date,
        )
        insert_planned_request(
            self.db_path,
            response_id=1113,
            full_name="Ночной Никита Иванович",
            full_name_key="ночной никита иванович",
            planned_date=locked_date,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', ?, ?, 'root', '2026-04-20T10:00:00', 'locked second segment')
                """,
                (locked_date, locked_date),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Ночной Никита Иванович"}).status_code)
            created = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": planned_date,
                    "planned_work_time": "23:00 - 05:00",
                    "payment_type": "Отгул",
                    "task_description": "Ночной релиз",
                    "justification": "Окно сопровождения",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, created.status_code)
        self.assertIn("прием заявок за этот период закрыт", unquote(created.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT response_id
                FROM survey_responses
                WHERE full_name_key = ?
                  AND task_description = 'Ночной релиз'
                """,
                ("ночной никита иванович",),
            ).fetchall()
            states = conn.execute(
                """
                SELECT response_id
                FROM app_request_state
                WHERE full_name_key = ?
                  AND override_task_description = 'Ночной релиз'
                """,
                ("ночной никита иванович",),
            ).fetchall()

        self.assertEqual(0, len(rows))
        self.assertEqual(0, len(states))

    def test_employee_actual_time_uses_planned_date_and_rejects_overnight(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=1116,
            full_name="Фактилов Федор Иванович",
            full_name_key="фактилов федор иванович",
            planned_date="2026-04-22",
        )
        insert_planned_request(
            self.db_path,
            response_id=1117,
            full_name="Фактилов Федор Иванович",
            full_name_key="фактилов федор иванович",
            planned_date="2026-04-23",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:1116', 1116, 'фактилов федор иванович', 'in_progress', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:1117', 1117, 'фактилов федор иванович', 'in_progress', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Фактилов Федор Иванович"}).status_code)
            saved = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "фактилов федор иванович",
                    "response_id": "1116",
                    "actual_work_date": "2026-04-30",
                    "actual_work_time": "10:00 - 12:00",
                },
                follow_redirects=False,
            )
            rejected = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "фактилов федор иванович",
                    "response_id": "1117",
                    "actual_work_date": "2026-04-30",
                    "actual_work_time": "23:00 - 05:00",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, saved.status_code)
        self.assertIn("фактическое время сохранено", unquote(saved.headers["location"]).lower())
        self.assertEqual(303, rejected.status_code)
        self.assertIn("через полночь", unquote(rejected.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            actual_row = conn.execute(
                "SELECT actual_work_date, actual_work_time FROM app_request_state WHERE response_id = 1116"
            ).fetchone()
            rejected_row = conn.execute(
                "SELECT actual_work_date, actual_work_time FROM app_request_state WHERE response_id = 1117"
            ).fetchone()
        self.assertEqual(("2026-04-22", "10:00 - 12:00"), actual_row)
        self.assertEqual((None, None), rejected_row)

    def test_full_report_generation_requires_admin_auth(self) -> None:
        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            response = client.post(
                "/generate/full",
                data={"date_from": "2026-04-20", "date_to": "2026-04-26"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertIn("администратор", unquote(response.headers["location"]).lower())

    def test_admin_can_login_and_generate_weekly_report(self) -> None:
        with (
            patch.dict("os.environ", SUPERUSER_ENV),
            patch.object(web_ui, "DB_PATH", self.db_path),
            patch.object(web_ui, "run_script") as run_script_mock,
        ):
            client = TestClient(web_ui.app)

            login = login_superuser(client)
            self.assertEqual(303, login.status_code)

            response = client.post(
                "/generate/full",
                data={"date_from": "2026-04-20", "date_to": "2026-04-26"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        decoded_location = unquote(response.headers["location"]).lower()
        self.assertTrue(decoded_location.startswith("/admin?"))
        self.assertIn("сформирован", decoded_location)
        run_script_mock.assert_called_once()

    def test_index_and_employee_pages_render(self) -> None:
        planned_date = (future_week_start(30) + web_ui.timedelta(days=2)).isoformat()
        insert_planned_request(
            self.db_path,
            response_id=404,
            full_name="Тестов Тест Тестович",
            full_name_key="тестов тест тестович",
            planned_date=planned_date,
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            index_response = client.get("/")
            admin_response = client.get("/admin", follow_redirects=False)
            login_response = client.post("/employee/login", data={"full_name": "Тестов Тест Тестович"})
            employee_response = client.get("/employee")

        self.assertEqual(200, index_response.status_code)
        self.assertIn("Work on Holiday", index_response.text)
        self.assertIn("Кабинет сотрудника", index_response.text)
        self.assertIn("Первичная настройка", index_response.text)
        self.assertIn('href="/employee"', index_response.text)
        self.assertNotIn('href="/admin"', index_response.text)
        self.assertIn('data-hamburger-menu="true"', index_response.text)
        self.assertEqual(303, admin_response.status_code)
        self.assertIn("только администратору", unquote(admin_response.headers["location"]).lower())
        self.assertEqual(200, login_response.status_code)
        self.assertIn("Токен сотрудника создан", login_response.text)
        self.assertEqual(200, employee_response.status_code)
        self.assertIn("Кабинет сотрудника", employee_response.text)
        self.assertIn('class="tooltip"', employee_response.text)
        self.assertIn('placeholder="ДД/ММ/ГГГГ"', employee_response.text)
        self.assertIn('data-date-picker="true"', employee_response.text)
        self.assertIn('data-time-mask="true"', employee_response.text)
        self.assertIn('data-time-start', employee_response.text)
        self.assertIn('data-time-end', employee_response.text)
        self.assertIn('data-overnight-preview', employee_response.text)
        self.assertIn('data-systems-editor', employee_response.text)
        self.assertIn("После ввода одной АС нажмите Enter или Tab", employee_response.text)
        self.assertIn('data-hamburger-menu="true"', employee_response.text)
        self.assertNotIn("data-emergency", employee_response.text)
        self.assertIn("<details", employee_response.text)
        self.assertIn("Создать новую заявку", employee_response.text)
        self.assertIn("Мои заявки", employee_response.text)
        self.assertIn("summary-row", employee_response.text)
        self.assertIn(web_ui.to_ru_date(planned_date), employee_response.text)
        self.assertIn(web_ui.to_ru_weekday(planned_date), employee_response.text.lower())
        self.assertIn("09:00 - 18:00", employee_response.text)

    def test_upload_route_is_removed_with_etl(self) -> None:
        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            response = client.post(
                "/upload",
                files={"file": ("dummy.xlsx", b"test", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                follow_redirects=False,
            )

        self.assertEqual(404, response.status_code)

    def test_cancelled_request_hides_actual_save_and_cancel_buttons(self) -> None:
        planned_date = (future_week_start(30) + web_ui.timedelta(days=2)).isoformat()
        insert_planned_request(
            self.db_path,
            response_id=408,
            full_name="Отклонов Олег Иванович",
            full_name_key="отклонов олег иванович",
            planned_date=planned_date,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES (
                    'req:408', 408, 'отклонов олег иванович', 'cancelled',
                    '2026-04-20T10:00:00', '2026-04-20T10:00:00'
                )
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login_response = client.post("/employee/login", data={"full_name": "Отклонов Олег Иванович"})
            employee_response = client.get("/employee")

        self.assertEqual(200, login_response.status_code)
        self.assertEqual(200, employee_response.status_code)
        self.assertIn("Отменена", employee_response.text)
        self.assertIn("Фактически отработанное время", employee_response.text)
        self.assertNotIn("Сохранить фактическое время", employee_response.text)
        self.assertNotIn('Перевести в статус "Отменена"', employee_response.text)

    def test_active_request_hides_actual_form_and_shows_reason(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=1115,
            full_name="Активов Андрей Иванович",
            full_name_key="активов андрей иванович",
            planned_date="2026-04-22",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Активов Андрей Иванович"}).status_code)
            page = client.get("/employee")

        self.assertEqual(200, page.status_code)
        self.assertIn("Фактически отработанное время", page.text)
        self.assertNotIn('action="/employee/request/actual"', page.text)
        self.assertIn("Ввод фактического времени сейчас недоступен.", page.text)
        self.assertNotIn("Сохранить фактическое время", page.text)

    def test_employee_first_login_issues_token_and_second_login_requires_token(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=505,
            full_name="Смирнов Иван Олегович",
            full_name_key="смирнов иван олегович",
            planned_date="2026-04-22",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)

            first_login = client.post(
                "/employee/login",
                data={"full_name": "Смирнов Иван Олегович", "grade_12_plus": "1"},
            )
            self.assertEqual(200, first_login.status_code)
            self.assertIn("Токен сотрудника создан", first_login.text)
            self.assertIn("Грейд 12+", first_login.text)
            self.assertIn("Да", first_login.text)

            with sqlite3.connect(self.db_path) as conn:
                stored_grade = conn.execute(
                    "SELECT grade_12_plus FROM app_employee_profile WHERE full_name_key = ?",
                    ("смирнов иван олегович",),
                ).fetchone()[0]
            self.assertEqual(1, stored_grade)

            token = None
            for cookie in client.cookies.jar:
                if cookie.name == web_ui.EMPLOYEE_TOKEN_COOKIE_NAME:
                    token = cookie.value
                    break
            self.assertIsNotNone(token)

            second_login = client.post(
                "/employee/login",
                data={"full_name": "Смирнов Иван Олегович"},
                follow_redirects=False,
            )
            self.assertEqual(303, second_login.status_code)
            decoded_location = unquote(second_login.headers["location"]).lower()
            self.assertIn("токен", decoded_location)
            self.assertIn("pending_employee_key", decoded_location)

            client.cookies.clear()
            token_login = client.post(
                "/employee/login",
                data={"full_name": "Смирнов Иван Олегович", "access_token": token},
                follow_redirects=False,
            )
            self.assertEqual(303, token_login.status_code)
            self.assertIn("вход выполнен", unquote(token_login.headers["location"]).lower())

    def test_employee_grade_is_used_for_new_web_requests_and_admin_can_update_it(self) -> None:
        create_date = future_date_iso(31)
        insert_planned_request(
            self.db_path,
            response_id=515,
            full_name="Высоков Григорий Павлович",
            full_name_key="высоков григорий павлович",
            planned_date="2026-04-22",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            first_login = client.post(
                "/employee/login",
                data={"full_name": "Высоков Григорий Павлович", "grade_12_plus": "1"},
            )
            self.assertEqual(200, first_login.status_code)

            create_response = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": create_date,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Отгул",
                    "task_description": "Проверка профиля",
                    "justification": "Регламентное окно",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )
            self.assertEqual(303, create_response.status_code)

            with sqlite3.connect(self.db_path) as conn:
                stored_request_grade = conn.execute(
                    """
                    SELECT grade_12_plus
                    FROM survey_responses
                    WHERE full_name_key = ? AND task_description = ?
                    """,
                    ("высоков григорий павлович", "Проверка профиля"),
                ).fetchone()[0]
            self.assertEqual(1, stored_request_grade)

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login_superuser(client)
            update_response = client.post(
                "/admin/employee/grade",
                data={"employee_key": "высоков григорий павлович", "grade_12_plus": "0"},
                follow_redirects=False,
            )
            self.assertEqual(303, update_response.status_code)
            self.assertIn("грейда", unquote(update_response.headers["location"]).lower())
            users_page = client.get("/admin/users")

        self.assertEqual(200, users_page.status_code)
        self.assertIn("Грейд 12+", users_page.text)
        with sqlite3.connect(self.db_path) as conn:
            updated_grade = conn.execute(
                "SELECT grade_12_plus FROM app_employee_profile WHERE full_name_key = ?",
                ("высоков григорий павлович",),
            ).fetchone()[0]
        self.assertEqual(0, updated_grade)

    def test_high_grade_employee_cannot_choose_double_payment_for_new_request(self) -> None:
        blocked_date = future_date_iso(32)
        planned_seed_date = future_date_iso(45)
        insert_planned_request(
            self.db_path,
            response_id=516,
            full_name="Грейдов Денис Сергеевич",
            full_name_key="грейдов денис сергеевич",
            planned_date=planned_seed_date,
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            first_login = client.post(
                "/employee/login",
                data={"full_name": "Грейдов Денис Сергеевич", "grade_12_plus": "1"},
            )
            self.assertEqual(200, first_login.status_code)

            cabinet = client.get("/employee")
            self.assertEqual(200, cabinet.status_code)
            self.assertIn("Грейд 12+: Да", cabinet.text)
            self.assertNotIn('<option value="Двойная оплата">Двойная оплата</option>', cabinet.text)

            blocked = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": blocked_date,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Двойная оплата",
                    "task_description": "Недоступная оплата",
                    "justification": "Проверка ограничения",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )
            self.assertEqual(303, blocked.status_code)
            self.assertIn("двойная оплата недоступна", unquote(blocked.headers["location"]).lower())

            with sqlite3.connect(self.db_path) as conn:
                blocked_rows = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM survey_responses
                    WHERE full_name_key = ? AND task_description = ?
                    """,
                    ("грейдов денис сергеевич", "Недоступная оплата"),
                ).fetchone()[0]
            self.assertEqual(0, blocked_rows)

    def test_non_high_grade_employee_can_choose_double_payment_for_new_request(self) -> None:
        create_date = future_date_iso(33)
        planned_seed_date = future_date_iso(46)
        insert_planned_request(
            self.db_path,
            response_id=517,
            full_name="Оплатов Илья Сергеевич",
            full_name_key="оплатов илья сергеевич",
            planned_date=planned_seed_date,
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            first_login = client.post(
                "/employee/login",
                data={"full_name": "Оплатов Илья Сергеевич", "grade_12_plus": "0"},
            )
            self.assertEqual(200, first_login.status_code)

            cabinet = client.get("/employee")
            self.assertEqual(200, cabinet.status_code)
            self.assertIn('<option value="Двойная оплата" selected>Двойная оплата</option>', cabinet.text)

            create_response = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": create_date,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Двойная оплата",
                    "task_description": "Доступная оплата",
                    "justification": "Проверка доступности",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )
            self.assertEqual(303, create_response.status_code)
            self.assertIn("заявка создана", unquote(create_response.headers["location"]).lower())

            with sqlite3.connect(self.db_path) as conn:
                payment_type = conn.execute(
                    """
                    SELECT payment_type
                    FROM survey_responses
                    WHERE full_name_key = ? AND task_description = ?
                    """,
                    ("оплатов илья сергеевич", "Доступная оплата"),
                ).fetchone()[0]
            self.assertEqual("Двойная оплата", payment_type)

    def test_employee_cannot_create_past_request_but_admin_can(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=518,
            full_name="Прошлов Павел Сергеевич",
            full_name_key="прошлов павел сергеевич",
            planned_date=future_date_iso(34),
        )
        yesterday = (web_ui.date.today() - web_ui.timedelta(days=1)).isoformat()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Прошлов Павел Сергеевич"}).status_code)

            blocked = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": yesterday,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Двойная оплата",
                    "task_description": "Опоздавшая заявка",
                    "justification": "Проверка запрета",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, blocked.status_code)
        blocked_location = unquote(blocked.headers["location"]).lower()
        self.assertIn("прошедшую дату", blocked_location)
        self.assertIn("обратитесь к администратору", blocked_location)
        self.assertIn("create_open=1", blocked_location)

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            admin_client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(admin_client).status_code)
            created = admin_client.post(
                "/employee/request/create",
                data={
                    "employee_key": "прошлов павел сергеевич",
                    "admin_mode": "1",
                    "planned_work_date": yesterday,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Двойная оплата",
                    "task_description": "Опоздавшая заявка",
                    "justification": "Проверка запрета",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, created.status_code)
        self.assertIn("заявка создана", unquote(created.headers["location"]).lower())

    def test_duplicate_employee_request_is_rejected(self) -> None:
        planned_date = future_date_iso(35)
        insert_planned_request(
            self.db_path,
            response_id=519,
            full_name="Дублев Денис Сергеевич",
            full_name_key="дублев денис сергеевич",
            planned_date=planned_date,
        )

        request_data = {
            "planned_work_date": planned_date,
            "planned_work_time": "10:00 - 12:00",
            "payment_type": "Двойная оплата",
            "task_description": "Повторная заявка",
            "justification": "Проверка дубля",
            "systems": "Пуаро | ЕФС.Риск-решения",
        }

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Дублев Денис Сергеевич"}).status_code)

            created = client.post("/employee/request/create", data=request_data, follow_redirects=False)
            duplicate = client.post("/employee/request/create", data=request_data, follow_redirects=False)

        self.assertEqual(303, created.status_code)
        self.assertIn("заявка создана", unquote(created.headers["location"]).lower())
        self.assertEqual(303, duplicate.status_code)
        duplicate_location = unquote(duplicate.headers["location"]).lower()
        self.assertIn("такая заявка уже создана", duplicate_location)
        self.assertIn("create_open=1", duplicate_location)

        with sqlite3.connect(self.db_path) as conn:
            rows_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM survey_responses
                WHERE full_name_key = ? AND task_description = ?
                """,
                ("дублев денис сергеевич", "Повторная заявка"),
            ).fetchone()[0]
        self.assertEqual(1, rows_count)

    def test_duplicate_employee_request_by_correction_is_rejected(self) -> None:
        planned_date = future_date_iso(36)
        insert_planned_request(
            self.db_path,
            response_id=520,
            full_name="Правкин Павел Сергеевич",
            full_name_key="правкин павел сергеевич",
            planned_date=planned_date,
        )
        insert_planned_request(
            self.db_path,
            response_id=521,
            full_name="Правкин Павел Сергеевич",
            full_name_key="правкин павел сергеевич",
            planned_date=future_date_iso(37),
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE survey_responses
                SET planned_work_time = '10:00 - 12:00',
                    payment_type = 'Двойная оплата',
                    task_description = 'Эталонная заявка',
                    justification = 'Проверка дубля'
                WHERE response_id = 520;
                """
            )
            conn.execute(
                """
                UPDATE survey_responses
                SET planned_work_time = '13:00 - 15:00',
                    payment_type = 'Двойная оплата',
                    task_description = 'Другая заявка',
                    justification = 'Проверка дубля'
                WHERE response_id = 521;
                """
            )
            conn.execute("DELETE FROM response_systems WHERE response_id IN (520, 521)")
            conn.executemany(
                "INSERT INTO response_systems (response_id, system_order, system_name) VALUES (?, ?, ?)",
                [(520, 1, "Пуаро"), (520, 2, "ЕФС.Риск-решения"), (521, 1, "Пуаро")],
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Правкин Павел Сергеевич"}).status_code)
            duplicate = client.post(
                "/employee/request/correct",
                data={
                    "employee_key": "правкин павел сергеевич",
                    "response_id": "521",
                    "planned_work_date": planned_date,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Двойная оплата",
                    "task_description": "Эталонная заявка",
                    "justification": "Проверка дубля",
                    "systems": "Пуаро | ЕФС.Риск-решения",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, duplicate.status_code)
        duplicate_location = unquote(duplicate.headers["location"]).lower()
        self.assertIn("такая заявка уже создана", duplicate_location)

        with sqlite3.connect(self.db_path) as conn:
            override = conn.execute(
                "SELECT override_task_description FROM app_request_state WHERE response_id = 521",
            ).fetchone()
        self.assertIsNone(override)

    def test_admin_can_reissue_employee_token(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=606,
            full_name="Козлов Антон Сергеевич",
            full_name_key="козлов антон сергеевич",
            planned_date="2026-04-22",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            first_login = client.post("/employee/login", data={"full_name": "Козлов Антон Сергеевич"})
            self.assertEqual(200, first_login.status_code)

            old_token = None
            for cookie in client.cookies.jar:
                if cookie.name == web_ui.EMPLOYEE_TOKEN_COOKIE_NAME:
                    old_token = cookie.value
                    break
            self.assertIsNotNone(old_token)

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = login_superuser(client)
            self.assertEqual(303, login.status_code)

            reissue = client.post(
                "/admin/employee/reissue-token",
                data={"employee_key": "козлов антон сергеевич"},
            )
            self.assertEqual(200, reissue.status_code)
            self.assertIn("перевыпущен", reissue.text.lower())

            token_marker = '<div class="token">'
            start = reissue.text.index(token_marker) + len(token_marker)
            end = reissue.text.index("</div>", start)
            new_token = reissue.text[start:end].strip()

            employee_client = TestClient(web_ui.app)
            old_token_login = employee_client.post(
                "/employee/login",
                data={"full_name": "Козлов Антон Сергеевич", "access_token": old_token},
                follow_redirects=False,
            )
            self.assertEqual(303, old_token_login.status_code)
            self.assertIn("устарел", unquote(old_token_login.headers["location"]).lower())

            new_token_login = employee_client.post(
                "/employee/login",
                data={"full_name": "Козлов Антон Сергеевич", "access_token": new_token},
                follow_redirects=False,
            )
            self.assertEqual(303, new_token_login.status_code)
            self.assertIn("вход выполнен", unquote(new_token_login.headers["location"]).lower())

    def test_admin_can_reissue_token_for_generated_directory_employee(self) -> None:
        users = generate_users.generate_users(self.db_path, count=1, seed=2026)
        employee = users[0]

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = login_superuser(client)
            self.assertEqual(303, login.status_code)

            reissue = client.post(
                "/admin/employee/reissue-token",
                data={"employee_key": employee.full_name_key},
            )
            self.assertEqual(200, reissue.status_code)
            self.assertIn("перевыпущен", reissue.text.lower())
            self.assertIn(employee.full_name, reissue.text)

        with sqlite3.connect(self.db_path) as conn:
            token_row = conn.execute(
                """
                SELECT token_hash, token_reissued_at, forgot_requested_at
                FROM app_employee_auth
                WHERE full_name_key = ?;
                """,
                (employee.full_name_key,),
            ).fetchone()

        self.assertIsNotNone(token_row)
        self.assertTrue(token_row[0])
        self.assertIsNotNone(token_row[1])
        self.assertIsNone(token_row[2])

    def test_unknown_employee_is_registered_and_receives_first_token(self) -> None:
        full_name = "Новиков Роман Сергеевич"
        full_name_key = "новиков роман сергеевич"

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            response = client.post(
                "/employee/login",
                data={"full_name": full_name, "grade_12_plus": "1"},
            )
            self.assertEqual(200, response.status_code)
            self.assertIn("Токен сотрудника создан", response.text)
            self.assertIn(full_name, response.text)
            self.assertIn("Грейд 12+", response.text)

            token_marker = '<div class="token">'
            start = response.text.index(token_marker) + len(token_marker)
            end = response.text.index("</div>", start)
            token = response.text[start:end].strip()

            login_response = client.post(
                "/employee/login",
                data={"full_name": full_name, "access_token": token},
                follow_redirects=False,
            )
            self.assertEqual(303, login_response.status_code)
            self.assertIn("вход выполнен", unquote(login_response.headers["location"]).lower())

        with sqlite3.connect(self.db_path) as conn:
            directory_row = conn.execute(
                """
                SELECT full_name, work_email, mobile_phone
                FROM app_employee_directory
                WHERE full_name_key = ?;
                """,
                (full_name_key,),
            ).fetchone()
            profile_row = conn.execute(
                """
                SELECT grade_12_plus, employee_status
                FROM app_employee_profile
                WHERE full_name_key = ?;
                """,
                (full_name_key,),
            ).fetchone()
            auth_row = conn.execute(
                """
                SELECT token_hash
                FROM app_employee_auth
                WHERE full_name_key = ?;
                """,
                (full_name_key,),
            ).fetchone()

        self.assertEqual((full_name, None, None), directory_row)
        self.assertEqual((1, "active"), profile_row)
        self.assertIsNotNone(auth_row)
        self.assertTrue(auth_row[0])

    def test_employee_can_mark_forgot_token_for_admin(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=808,
            full_name="Федоров Илья Викторович",
            full_name_key="федоров илья викторович",
            planned_date="2026-04-22",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            first_login = client.post("/employee/login", data={"full_name": "Федоров Илья Викторович"})
            self.assertEqual(200, first_login.status_code)

            forgot = client.post(
                "/employee/forgot-token",
                data={"full_name": "Федоров Илья Викторович"},
                follow_redirects=False,
            )
            self.assertEqual(303, forgot.status_code)
            forgot_location = unquote(forgot.headers["location"]).lower()
            self.assertIn("за получением нового токена обратитесь к администратору", forgot_location)
            self.assertIn("level=token-warning", forgot_location)

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login_superuser(client)
            users_page = client.get("/admin/users")

        self.assertEqual(200, users_page.status_code)
        self.assertIn("Федоров Илья Викторович", users_page.text)
        self.assertIn("Да,", users_page.text)

    def test_admin_navigation_pages_are_protected_and_available_after_login(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=707,
            full_name="Орлов Денис Петрович",
            full_name_key="орлов денис петрович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date,
                    actual_work_time, created_at, updated_at
                ) VALUES (
                    'req:707', 707, 'орлов денис петрович', 'in_fact',
                    '2026-04-22', '09:00 - 18:00', '2026-04-20T10:00:00', '2026-04-20T10:00:00'
                )
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            denied = client.get("/admin/users", follow_redirects=False)
            self.assertEqual(303, denied.status_code)
            self.assertIn("только администратору", unquote(denied.headers["location"]).lower())

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = login_superuser(client)
            self.assertEqual(303, login.status_code)

            index_response = client.get("/")
            admin_response = client.get("/admin")
            users_response = client.get("/admin/users")
            requests_response = client.get("/admin/requests")
            test_data_response = client.get("/admin/test-data")

        self.assertEqual(200, index_response.status_code)
        self.assertIn('href="/admin"', index_response.text)
        self.assertIn('data-hamburger-menu="true"', index_response.text)
        self.assertEqual(200, admin_response.status_code)
        self.assertIn('placeholder="ДД/ММ/ГГГГ"', admin_response.text)
        self.assertIn('data-date-picker="true"', admin_response.text)
        self.assertIn("/admin/users", admin_response.text)
        self.assertIn("/admin/requests", admin_response.text)
        self.assertIn("/admin/test-data", admin_response.text)
        self.assertIn("Общий Excel с отчетами 1-4", admin_response.text)
        self.assertIn("Закрыть неделю", admin_response.text)
        self.assertNotIn('/generate/actual', admin_response.text)
        self.assertNotIn('/generate/reconciliation', admin_response.text)
        self.assertIn('data-hamburger-menu="true"', admin_response.text)
        self.assertEqual(200, users_response.status_code)
        self.assertIn("Пользователи", users_response.text)
        self.assertIn("Открыть кабинет", users_response.text)
        self.assertIn('data-hamburger-menu="true"', users_response.text)
        self.assertEqual(200, requests_response.status_code)
        self.assertIn("Заявки", requests_response.text)
        self.assertIn('aria-label="Фильтры заявок"', requests_response.text)
        self.assertIn('id="request-search"', requests_response.text)
        self.assertIn('id="request-status-filter"', requests_response.text)
        self.assertIn('id="request-date-filter"', requests_response.text)
        self.assertIn("Открыть кабинет", requests_response.text)
        self.assertIn('class="task-preview"', requests_response.text)
        self.assertIn('data-delete-request', requests_response.text)
        self.assertIn('id="delete-request-dialog"', requests_response.text)
        self.assertIn('action="/admin/request/delete"', requests_response.text)
        self.assertIn('data-hamburger-menu="true"', requests_response.text)
        self.assertIn("22.04.2026", requests_response.text)
        self.assertEqual(200, test_data_response.status_code)
        self.assertIn("Генерация тестовых данных", test_data_response.text)
        self.assertIn('maxlength="500"', test_data_response.text)
        self.assertIn('data-date-picker="true"', test_data_response.text)
        self.assertIn('data-time-mask="true"', test_data_response.text)
        self.assertIn('data-hamburger-menu="true"', test_data_response.text)

    def test_date_picker_keeps_popover_open_on_internal_clicks(self) -> None:
        shared_component = (web_ui.TEMPLATES_DIR / "includes" / "date_time_controls.js").read_text(encoding="utf-8")
        self.assertIn('popover.addEventListener("click"', shared_component)
        self.assertIn("event.stopPropagation();", shared_component)
        self.assertIn('prev.addEventListener("click"', shared_component)
        self.assertIn('next.addEventListener("click"', shared_component)
        for template_name in ("employee.html", "index.html", "admin_test_data.html"):
            template_text = (web_ui.TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn('includes/date_time_controls.js', template_text)

    def test_admin_test_data_page_requires_admin_and_generates_in_fact_batch(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=801,
            full_name="Тестов Тимур Иванович",
            full_name_key="тестов тимур иванович",
            planned_date="2026-05-10",
        )
        insert_planned_request(
            self.db_path,
            response_id=802,
            full_name="Грейдов Григорий Иванович",
            full_name_key="грейдов григорий иванович",
            planned_date="2026-05-10",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_employee_profile (full_name_key, grade_12_plus, updated_at)
                VALUES (?, 1, '2026-05-20T10:00:00')
                """,
                ("грейдов григорий иванович",),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, payment_type, task_description, justification,
                    planned_work_date, planned_work_time, target_work_date, source_file
                ) VALUES (
                    899, 899, '2026-05-20T10:00:00', 'Старый Тестовый Пользователь',
                    'Старый Тестовый Пользователь', 'старый тестовый пользователь',
                    'Подать заявку', 'Отгул', 'old', 'old', '2026-05-23', '09:00 - 10:00',
                    '2026-05-23', 'admin_test_data:2026-05-23:old'
                )
                """
            )
            conn.execute(
                "INSERT INTO response_systems (response_id, system_order, system_name) VALUES (899, 1, 'Old')"
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date,
                    actual_work_time, created_at, updated_at
                ) VALUES (
                    'req:899', 899, 'старый тестовый пользователь', 'completed',
                    '2026-05-23', '09:00 - 10:00', '2026-05-20T10:00:00', '2026-05-20T10:00:00'
                )
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            anonymous_client = TestClient(web_ui.app)
            denied = anonymous_client.get("/admin/test-data", follow_redirects=False)

        self.assertEqual(303, denied.status_code)
        self.assertIn("администратору", unquote(denied.headers["location"]).lower())

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            page = client.get("/admin/test-data")
            created = client.post(
                "/admin/test-data",
                data={
                    "test_work_date": "2026-05-23",
                    "generation_mode": "plan_and_actual",
                    "employee_keys": ["тестов тимур иванович", "грейдов григорий иванович"],
                    "planned_work_time": "10:00 - 14:00",
                    "task_description": "сопровождение релиза",
                    "justification": "технологическое окно",
                    "systems": "Пуаро | ЕФС.Риск-решения",
                },
                follow_redirects=False,
            )

        self.assertEqual(200, page.status_code)
        self.assertIn("Генерация тестовых данных", page.text)
        self.assertIn("Тестов Тимур Иванович", page.text)
        self.assertEqual(303, created.status_code)
        self.assertIn("/admin/test-data", created.headers["location"])
        self.assertIn("создано", unquote(created.headers["location"]).lower())

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            generated_rows = conn.execute(
                """
                SELECT response_id, full_name_key, payment_type, planned_work_date, planned_work_time, source_file
                FROM survey_responses
                WHERE planned_work_date = '2026-05-23'
                  AND source_file LIKE 'admin_test_data:2026-05-23:%'
                ORDER BY full_name_key
                """
            ).fetchall()
            old_row = conn.execute("SELECT 1 FROM survey_responses WHERE response_id = 899").fetchone()
            old_system = conn.execute("SELECT 1 FROM response_systems WHERE response_id = 899").fetchone()
            old_state = conn.execute("SELECT 1 FROM app_request_state WHERE response_id = 899").fetchone()
            states = conn.execute(
                """
                SELECT response_id, status, actual_work_date, actual_work_time
                FROM app_request_state
                WHERE response_id IN (?, ?)
                ORDER BY response_id
                """,
                (generated_rows[0]["response_id"], generated_rows[1]["response_id"]),
            ).fetchall()
            systems = conn.execute(
                """
                SELECT response_id, GROUP_CONCAT(system_name, ' | ') AS systems
                FROM response_systems
                WHERE response_id IN (?, ?)
                GROUP BY response_id
                ORDER BY response_id
                """,
                (generated_rows[0]["response_id"], generated_rows[1]["response_id"]),
            ).fetchall()

        self.assertEqual(2, len(generated_rows))
        self.assertIsNone(old_row)
        self.assertIsNone(old_system)
        self.assertIsNone(old_state)
        payments = {row["full_name_key"]: row["payment_type"] for row in generated_rows}
        self.assertEqual("Двойная оплата", payments["тестов тимур иванович"])
        self.assertEqual("Отгул", payments["грейдов григорий иванович"])
        self.assertTrue(all(row["planned_work_time"] == "10:00 - 14:00" for row in generated_rows))
        self.assertEqual(2, len(states))
        self.assertTrue(all(row["status"] == "in_fact" for row in states))
        self.assertTrue(all(row["actual_work_date"] == "2026-05-23" for row in states))
        self.assertTrue(all(row["actual_work_time"] == "10:00 - 14:00" for row in states))
        self.assertEqual(["Пуаро | ЕФС.Риск-решения", "Пуаро | ЕФС.Риск-решения"], [row["systems"] for row in systems])

    def test_admin_test_data_rejects_invalid_values_without_creating_rows(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=1201,
            full_name="Админов Андрей Иванович",
            full_name_key="админов андрей иванович",
            planned_date="2026-05-23",
        )

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            cases = [
                ("date_partial", {"test_work_date": "2026-05"}, "Некорректная дата тестовых заявок"),
                ("date_nonexistent", {"test_work_date": "2026-02-30"}, "Некорректная дата тестовых заявок"),
                ("time_overnight", {"planned_work_time": "23:00 - 05:00"}, "Ночной интервал нельзя создать одной тестовой заявкой"),
                ("mode_invalid", {"generation_mode": "bogus"}, "Некорректный режим генерации"),
                ("time_invalid", {"planned_work_time": "10:00 -"}, "Некорректный формат времени"),
                ("employee_keys_empty", {"employee_keys": []}, "Выберите хотя бы одного сотрудника"),
                ("systems_empty", {"systems": "   "}, "Укажите хотя бы одну АС"),
                ("task_empty", {"task_description": ""}, "Укажите задачу"),
                (
                    "task_too_long",
                    {"task_description": "А" * 501},
                    "Задача не может быть длиннее 500 символов",
                ),
                ("justification_empty", {"justification": ""}, "Укажите обоснование"),
            ]

            for case_name, overrides, expected_message in cases:
                with self.subTest(case=case_name):
                    response = client.post(
                        "/admin/test-data",
                        data={
                            "test_work_date": "2026-05-23",
                            "generation_mode": "plan_and_actual",
                            "employee_keys": ["админов андрей иванович"],
                            "planned_work_time": "10:00 - 14:00",
                            "task_description": "сопровождение релиза",
                            "justification": "технологическое окно",
                            "systems": "Пуаро | ЕФС.Риск-решения",
                            **overrides,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(303, response.status_code)
                    self.assertIn(expected_message.lower(), unquote(response.headers["location"]).lower())

                    with sqlite3.connect(self.db_path) as conn:
                        generated_rows = conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM survey_responses
                            WHERE source_file LIKE 'admin_test_data:%'
                            """
                        ).fetchone()[0]
                        old_row = conn.execute(
                            "SELECT 1 FROM survey_responses WHERE response_id = 1201"
                        ).fetchone()
                        self.assertEqual(0, generated_rows)
                        self.assertIsNotNone(old_row)
                        web_ui.delete_admin_test_data_for_date(conn, "2026-05-23")
                        conn.commit()

    def test_employee_request_correction_rejects_invalid_input_without_side_effects(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=925,
            full_name="Проверкин Павел Иванович",
            full_name_key="проверкин павел иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status,
                    override_planned_work_date, override_planned_work_time,
                    override_payment_type, override_task_description,
                    override_justification, override_systems, created_at, updated_at
                ) VALUES (
                    'req:925', 925, 'проверкин павел иванович', 'active',
                    '2026-04-24', '09:00 - 18:00',
                    'Отгул', 'Старая задача',
                    'Старое обоснование', 'Система A', '2026-04-20T10:00:00', '2026-04-20T10:00:00'
                )
                """
            )
            conn.commit()
            initial_state = conn.execute(
                """
                SELECT
                    status,
                    override_planned_work_date,
                    override_planned_work_time,
                    override_payment_type,
                    override_task_description,
                    override_justification,
                    override_systems
                FROM app_request_state
                WHERE response_id = 925
                """
            ).fetchone()

        cases = [
            ("date_nonexistent", {"planned_work_date": "2026-02-30"}, "Некорректная плановая дата"),
            ("date_partial", {"planned_work_date": "2026-04"}, "Некорректная плановая дата"),
            ("time_invalid_hour", {"planned_work_time": "33:00 - 34:00"}, "Некорректное время"),
            ("time_zero_interval", {"planned_work_time": "10:00 - 10:00"}, "Продолжительность рабочего интервала должна быть больше нуля"),
            (
                "time_overnight",
                {"planned_work_time": "23:00 - 05:00"},
                "Ночной интервал оформляется двумя заявками. Создайте новую заявку с нужным интервалом.",
            ),
            (
                "payment_unknown",
                {"payment_type": "Бонус"},
                "Некорректный тип компенсации. Допустимые значения: Отгул, Двойная оплата",
            ),
            ("task_empty", {"task_description": ""}, "Укажите задачу"),
            (
                "task_too_long",
                {"task_description": "А" * 501},
                "Задача не может быть длиннее 500 символов",
            ),
            ("justification_empty", {"justification": ""}, "Укажите обоснование"),
            ("systems_empty", {"systems": "   "}, "Укажите хотя бы одну систему"),
        ]

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Проверкин Павел Иванович"}).status_code)

            for case_name, overrides, expected_message in cases:
                with self.subTest(case=case_name):
                    response = client.post(
                        "/employee/request/correct",
                        data={
                            "employee_key": "проверкин павел иванович",
                            "response_id": "925",
                            "planned_work_date": future_date_iso(30),
                            "planned_work_time": "10:00 - 12:00",
                            "payment_type": "Отгул",
                            "task_description": "Новая задача",
                            "justification": "Новое обоснование",
                            "systems": "Система B",
                            **overrides,
                        },
                        follow_redirects=False,
                    )

                    self.assertEqual(303, response.status_code)
                    self.assertIn(expected_message.lower(), unquote(response.headers["location"]).lower())

                    with sqlite3.connect(self.db_path) as conn:
                        current_state = conn.execute(
                            """
                            SELECT
                                status,
                                override_planned_work_date,
                                override_planned_work_time,
                                override_payment_type,
                                override_task_description,
                                override_justification,
                                override_systems
                            FROM app_request_state
                            WHERE response_id = 925
                            """
                        ).fetchone()
                    self.assertEqual(initial_state, current_state)

    def test_app_tables_include_user_roles_status_and_period_locks(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            profile_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(app_employee_profile)").fetchall()
            }
            self.assertIn("is_admin", profile_columns)
            self.assertIn("is_superuser", profile_columns)
            self.assertIn("employee_status", profile_columns)
            self.assertIn("status_reason", profile_columns)

            request_state_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(app_request_state)").fetchall()
            }
            request_state_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'app_request_state'",
            ).fetchone()[0]
            self.assertIn("returned_for_correction", request_state_columns)
            self.assertIn("in_progress", request_state_sql)
            self.assertIn("in_fact", request_state_sql)
            self.assertIn("blocked_at", profile_columns)
            self.assertIn("archived_at", profile_columns)
            self.assertIn("restored_at", profile_columns)
            self.assertIn("updated_by", profile_columns)

            lock_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(app_period_lock)").fetchall()
            }
            self.assertEqual(
                {
                    "lock_id",
                    "lock_type",
                    "date_from",
                    "date_to",
                    "created_by",
                    "created_at",
                    "comment",
                },
                lock_columns,
            )

    def test_legacy_admin_login_route_is_removed(self) -> None:
        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            response = client.post("/admin/login", data={"admin_token": "secret"})

        self.assertEqual(404, response.status_code)

    def test_employee_with_admin_flag_can_open_admin_dashboard(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=901,
            full_name="Админов Андрей Иванович",
            full_name_key="админов андрей иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_employee_profile (full_name_key, grade_12_plus, is_admin, updated_at)
                VALUES (?, 0, 1, '2026-04-20T10:00:00')
                """,
                ("админов андрей иванович",),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = client.post("/employee/login", data={"full_name": "Админов Андрей Иванович"})
            self.assertEqual(200, login.status_code)
            admin_page = client.get("/admin")

        self.assertEqual(200, admin_page.status_code)
        self.assertIn("Кабинет администратора", admin_page.text)

    def test_regular_employee_cannot_open_admin_dashboard(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=902,
            full_name="Рядовой Роман Иванович",
            full_name_key="рядовой роман иванович",
            planned_date="2026-04-22",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = client.post("/employee/login", data={"full_name": "Рядовой Роман Иванович"})
            self.assertEqual(200, login.status_code)
            admin_page = client.get("/admin", follow_redirects=False)

        self.assertEqual(303, admin_page.status_code)
        self.assertIn("администратору", unquote(admin_page.headers["location"]).lower())

    def test_superuser_can_assign_admin_role_to_employee(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=903,
            full_name="Ролевой Роман Иванович",
            full_name_key="ролевой роман иванович",
            planned_date="2026-04-22",
        )

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            update = client.post(
                "/admin/employee/admin-role",
                data={"employee_key": "ролевой роман иванович", "is_admin": "1"},
                follow_redirects=False,
            )

        self.assertEqual(303, update.status_code)
        with sqlite3.connect(self.db_path) as conn:
            role = conn.execute(
                "SELECT is_admin FROM app_employee_profile WHERE full_name_key = ?",
                ("ролевой роман иванович",),
            ).fetchone()[0]
        self.assertEqual(1, role)

    def test_secure_cookie_env_marks_superuser_cookie_secure(self) -> None:
        with patch.dict("os.environ", SECURE_SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            response = login_superuser(client)

        self.assertEqual(303, response.status_code)
        self.assertIn("secure", response.headers["set-cookie"].lower())

    def test_planning_period_lock_blocks_employee_create_but_not_admin(self) -> None:
        week_start = future_week_start(40)
        week_end = week_start + web_ui.timedelta(days=6)
        locked_date = (week_start + web_ui.timedelta(days=5)).isoformat()
        insert_planned_request(
            self.db_path,
            response_id=904,
            full_name="Локов Лев Иванович",
            full_name_key="локов лев иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', ?, ?, 'root', ?, 'test')
                """,
                (week_start.isoformat(), week_end.isoformat(), f"{week_start.isoformat()}T10:00:00"),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Локов Лев Иванович"}).status_code)
            blocked = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": locked_date,
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Отгул",
                    "task_description": "Закрытый период",
                    "justification": "Проверка",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, blocked.status_code)
        self.assertIn("прием заявок", unquote(blocked.headers["location"]).lower())

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            admin_client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(admin_client).status_code)
            created = admin_client.post(
                "/employee/request/create",
                data={
                    "employee_key": "локов лев иванович",
                    "admin_mode": "1",
                    "planned_work_date": locked_date,
                    "planned_work_time": "13:00 - 15:00",
                    "payment_type": "Отгул",
                    "task_description": "Админская заявка",
                    "justification": "Проверка",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, created.status_code)
        self.assertIn("заявка создана", unquote(created.headers["location"]).lower())

    def test_planning_period_lock_moves_active_requests_to_in_progress(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=916,
            full_name="Процессов Павел Иванович",
            full_name_key="процессов павел иванович",
            planned_date="2026-04-22",
        )
        insert_planned_request(
            self.db_path,
            response_id=917,
            full_name="Процессов Павел Иванович",
            full_name_key="процессов павел иванович",
            planned_date="2026-04-23",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:917', 917, 'процессов павел иванович', 'cancelled', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            response = client.post(
                "/admin/locks/create",
                data={
                    "lock_type": "planning",
                    "date_from": "2026-04-20",
                    "date_to": "2026-04-26",
                    "comment": "close planning",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as conn:
            statuses = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT response_id, status FROM app_request_state WHERE response_id IN (916, 917)"
                ).fetchall()
            }
        self.assertEqual("in_progress", statuses[916])
        self.assertEqual("cancelled", statuses[917])

    def test_employee_actual_time_moves_request_to_in_fact(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=918,
            full_name="Фактичев Федор Иванович",
            full_name_key="фактичев федор иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:918', 918, 'фактичев федор иванович', 'in_progress', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Фактичев Федор Иванович"}).status_code)
            response = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "фактичев федор иванович",
                    "response_id": "918",
                    "actual_work_date": "2026-04-22",
                    "actual_work_time": "10:00 - 12:00",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as conn:
            status = conn.execute("SELECT status FROM app_request_state WHERE response_id = 918").fetchone()[0]
        self.assertEqual("in_fact", status)

    def test_actual_period_lock_moves_in_fact_requests_to_completed(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=919,
            full_name="Закрытов Федор Иванович",
            full_name_key="закрытов федор иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date, actual_work_time, created_at, updated_at
                ) VALUES ('req:919', 919, 'закрытов федор иванович', 'in_fact', '2026-04-22', '10:00 - 12:00', '2026-04-22T12:00:00', '2026-04-22T12:00:00')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            response = client.post(
                "/admin/locks/create",
                data={
                    "lock_type": "actual",
                    "date_from": "2026-04-20",
                    "date_to": "2026-04-26",
                    "comment": "close fact",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as conn:
            status = conn.execute("SELECT status FROM app_request_state WHERE response_id = 919").fetchone()[0]
        self.assertEqual("completed", status)

    def test_admin_can_return_in_progress_request_to_active(self) -> None:
        corrected_date = future_date_iso(41)
        insert_planned_request(
            self.db_path,
            response_id=920,
            full_name="Возвратов Виктор Иванович",
            full_name_key="возвратов виктор иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', '2026-04-20', '2026-04-26', 'root', '2026-04-20T10:00:00', 'planning remains locked')
                """
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:920', 920, 'возвратов виктор иванович', 'in_progress', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            response = client.post(
                "/admin/request/status",
                data={
                    "employee_key": "возвратов виктор иванович",
                    "response_id": "920",
                    "status": "active",
                    "filter_name": "Возвратов",
                    "filter_status": "in_progress",
                    "filter_date": "22/04/2026",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        redirect_location = unquote(response.headers["location"])
        self.assertIn("filter_name=Возвратов", redirect_location)
        self.assertIn("filter_status=in_progress", redirect_location)
        self.assertIn("filter_date=22%2F04%2F2026", response.headers["location"])
        with sqlite3.connect(self.db_path) as conn:
            status, returned_for_correction = conn.execute(
                "SELECT status, returned_for_correction FROM app_request_state WHERE response_id = 920"
            ).fetchone()
        self.assertEqual("active", status)
        self.assertEqual(1, returned_for_correction)

        with patch.object(web_ui, "DB_PATH", self.db_path):
            employee_client = TestClient(web_ui.app)
            self.assertEqual(
                200,
                employee_client.post("/employee/login", data={"full_name": "Возвратов Виктор Иванович"}).status_code,
            )
            correction = employee_client.post(
                "/employee/request/correct",
                data={
                    "employee_key": "возвратов виктор иванович",
                    "response_id": "920",
                    "planned_work_date": corrected_date,
                    "planned_work_time": "11:00 - 13:00",
                    "payment_type": "Отгул",
                    "task_description": "Исправленная задача",
                    "justification": "Исправленная причина",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, correction.status_code)
        self.assertIn("заявка откорректирована", unquote(correction.headers["location"]).lower())

    def test_admin_request_delete_requires_arithmetic_answer_and_removes_related_records(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=921,
            full_name="Удаляев Илья Иванович",
            full_name_key="удаляев илья иванович",
            planned_date="2026-04-25",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES (
                    'req:921', 921, 'удаляев илья иванович', 'active',
                    '2026-04-20T10:00:00', '2026-04-20T10:00:00'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO app_report_lock (response_id, week_start, week_end, report_file, locked_at)
                VALUES (921, '2026-04-20', '2026-04-26', 'report.xlsx', '2026-04-24T18:00:00')
                """
            )
            conn.commit()

        payload = {
            "employee_key": "удаляев илья иванович",
            "response_id": "921",
            "challenge_left": "4",
            "challenge_right": "3",
            "challenge_answer": "7",
            "filter_name": "Удаляев",
            "filter_status": "active",
            "filter_date": "25/04/2026",
        }
        with patch.object(web_ui, "DB_PATH", self.db_path):
            anonymous_client = TestClient(web_ui.app)
            denied = anonymous_client.post("/admin/request/delete", data=payload, follow_redirects=False)

        self.assertEqual(303, denied.status_code)
        self.assertIn("только администратору", unquote(denied.headers["location"]).lower())

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            wrong_answer = client.post(
                "/admin/request/delete",
                data={**payload, "challenge_answer": "8"},
                follow_redirects=False,
            )
            deleted = client.post("/admin/request/delete", data=payload, follow_redirects=False)

        self.assertEqual(303, wrong_answer.status_code)
        self.assertIn("неверный ответ", unquote(wrong_answer.headers["location"]).lower())
        self.assertEqual(303, deleted.status_code)
        redirect_location = unquote(deleted.headers["location"])
        self.assertIn("filter_name=Удаляев", redirect_location)
        self.assertIn("filter_status=active", redirect_location)
        self.assertIn("filter_date=25%2F04%2F2026", deleted.headers["location"])
        self.assertIn("заявка удалена", redirect_location.lower())
        with sqlite3.connect(self.db_path) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM survey_responses WHERE response_id = 921").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM response_systems WHERE response_id = 921").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM app_request_state WHERE response_id = 921").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM app_report_lock WHERE response_id = 921").fetchone())

    def test_admin_invalid_status_transitions_and_completed_without_fact_are_rejected(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=1121,
            full_name="Статусов Степан Иванович",
            full_name_key="статусов степан иванович",
            planned_date="2026-04-22",
        )
        insert_planned_request(
            self.db_path,
            response_id=1122,
            full_name="Статусов Степан Иванович",
            full_name_key="статусов степан иванович",
            planned_date="2026-04-23",
        )
        insert_planned_request(
            self.db_path,
            response_id=1123,
            full_name="Статусов Степан Иванович",
            full_name_key="статусов степан иванович",
            planned_date="2026-04-24",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:1121', 1121, 'статусов степан иванович', 'active', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:1122', 1122, 'статусов степан иванович', 'cancelled', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:1123', 1123, 'статусов степан иванович', 'in_fact', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', '2026-04-20', '2026-04-26', 'root', '2026-04-20T10:00:00', 'locked')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            invalid = client.post(
                "/admin/request/status",
                data={"employee_key": "статусов степан иванович", "response_id": "1121", "status": "completed"},
                follow_redirects=False,
            )
            cancelled_restore = client.post(
                "/admin/request/status",
                data={"employee_key": "статусов степан иванович", "response_id": "1122", "status": "active"},
                follow_redirects=False,
            )
            completed_without_fact = client.post(
                "/admin/request/status",
                data={"employee_key": "статусов степан иванович", "response_id": "1123", "status": "completed"},
                follow_redirects=False,
            )

        self.assertEqual(303, invalid.status_code)
        self.assertIn("недопустимый переход статуса", unquote(invalid.headers["location"]).lower())
        self.assertEqual(303, cancelled_restore.status_code)
        self.assertIn("статус заявки обновлен", unquote(cancelled_restore.headers["location"]).lower())
        self.assertEqual(303, completed_without_fact.status_code)
        self.assertIn(
            "для статуса факт указан или закрыта обязательны дата и корректный интервал факта",
            unquote(completed_without_fact.headers["location"]).lower(),
        )
        with sqlite3.connect(self.db_path) as conn:
            invalid_status = conn.execute("SELECT status FROM app_request_state WHERE response_id = 1121").fetchone()[0]
            restored_status = conn.execute("SELECT status FROM app_request_state WHERE response_id = 1122").fetchone()[0]
        self.assertEqual("active", invalid_status)
        self.assertEqual("in_progress", restored_status)

    def test_admin_can_release_planning_lock_without_changing_request_status(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=916,
            full_name="Разлоков Роман Иванович",
            full_name_key="разлоков роман иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_id, lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES (501, 'planning', '2026-04-20', '2026-04-26', 'root', '2026-04-20T10:00:00', 'release test')
                """
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:916', 916, 'разлоков роман иванович', 'cancelled', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            admin_page = client.get("/admin")
            self.assertEqual(200, admin_page.status_code)
            self.assertIn("/admin/locks/release-planning", admin_page.text)

            release = client.post(
                "/admin/locks/release-planning",
                data={"lock_id": "501"},
                follow_redirects=False,
            )

        self.assertEqual(303, release.status_code)
        self.assertIn("блокировка приема заявок снята", unquote(release.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            lock_count = conn.execute("SELECT COUNT(*) FROM app_period_lock WHERE lock_id = 501").fetchone()[0]
            status = conn.execute(
                "SELECT status FROM app_request_state WHERE response_id = 916",
            ).fetchone()[0]
        self.assertEqual(0, lock_count)
        self.assertEqual("cancelled", status)

    def test_overlapping_same_type_week_lock_is_rejected(self) -> None:
        week_start = future_week_start(43)
        week_end = week_start + web_ui.timedelta(days=6)
        insert_planned_request(
            self.db_path,
            response_id=1114,
            full_name="Периодов Павел Иванович",
            full_name_key="периодов павел иванович",
            planned_date=(week_start + web_ui.timedelta(days=2)).isoformat(),
        )

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            preview = client.get(
                "/admin/locks/preview",
                params={"lock_type": "planning", "date_from": week_start.isoformat(), "date_to": week_end.isoformat()},
            )
            created = client.post(
                "/admin/locks/create",
                data={
                    "lock_type": "planning",
                    "date_from": week_start.isoformat(),
                    "date_to": week_end.isoformat(),
                    "comment": "first lock",
                },
                follow_redirects=False,
            )
            overlapping = client.post(
                "/admin/locks/create",
                data={
                    "lock_type": "planning",
                    "date_from": week_start.isoformat(),
                    "date_to": week_end.isoformat(),
                    "comment": "second lock",
                },
                follow_redirects=False,
            )

        self.assertEqual(200, preview.status_code)
        self.assertEqual({"count": 1, "overlap": False, "date_from": week_start.isoformat(), "date_to": week_end.isoformat()}, preview.json())
        self.assertEqual(303, created.status_code)
        self.assertIn("период приема заявок закрыт", unquote(created.headers["location"]).lower())
        self.assertEqual(303, overlapping.status_code)
        self.assertIn("пересекается", unquote(overlapping.headers["location"]).lower())

    def test_admin_cannot_release_actual_lock_through_planning_unlock(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_id, lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES (502, 'actual', '2026-04-20', '2026-04-26', 'root', '2026-04-20T10:00:00', 'actual lock')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            release = client.post(
                "/admin/locks/release-planning",
                data={"lock_id": "502"},
                follow_redirects=False,
            )

        self.assertEqual(303, release.status_code)
        self.assertIn("только блокировку приема заявок", unquote(release.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            lock_count = conn.execute("SELECT COUNT(*) FROM app_period_lock WHERE lock_id = 502").fetchone()[0]
        self.assertEqual(1, lock_count)

    def test_planning_period_lock_hides_employee_correction_form(self) -> None:
        week_start = future_week_start(42)
        week_end = week_start + web_ui.timedelta(days=6)
        locked_date = (week_start + web_ui.timedelta(days=2)).isoformat()
        insert_planned_request(
            self.db_path,
            response_id=914,
            full_name="Закрытов Захар Иванович",
            full_name_key="закрытов захар иванович",
            planned_date=locked_date,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', ?, ?, 'root', ?, 'test')
                """,
                (week_start.isoformat(), week_end.isoformat(), f"{week_start.isoformat()}T10:00:00"),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Закрытов Захар Иванович"}).status_code)
            page = client.get("/employee")

        self.assertEqual(200, page.status_code)
        self.assertIn("Прием заявок за этот период закрыт", page.text)
        self.assertNotIn('action="/employee/request/correct"', page.text)
        self.assertNotIn('action="/employee/request/actual"', page.text)
        self.assertIn("Ввод фактического времени сейчас недоступен.", page.text)

    def test_planning_period_lock_blocks_employee_correction_from_locked_current_date(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=915,
            full_name="Корректов Кирилл Иванович",
            full_name_key="корректов кирилл иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', '2026-04-20', '2026-04-26', 'root', '2026-04-20T10:00:00', 'test')
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Корректов Кирилл Иванович"}).status_code)
            blocked = client.post(
                "/employee/request/correct",
                data={
                    "employee_key": "корректов кирилл иванович",
                    "response_id": "915",
                    "planned_work_date": future_date_iso(43),
                    "planned_work_time": "10:00 - 12:00",
                    "payment_type": "Отгул",
                    "task_description": "Попытка переноса",
                    "justification": "Проверка",
                    "systems": "Система A",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, blocked.status_code)
        self.assertIn("корректировка доступна только администратору", unquote(blocked.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            state = conn.execute(
                "SELECT override_planned_work_date FROM app_request_state WHERE response_id = 915",
            ).fetchone()
        self.assertIsNone(state)

    def test_actual_period_lock_blocks_employee_actual_time(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=905,
            full_name="Фактов Федор Иванович",
            full_name_key="фактов федор иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:905', 905, 'фактов федор иванович', 'in_progress', '2026-04-20T10:00:00', '2026-04-20T10:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('actual', '2026-04-20', '2026-04-26', 'root', '2026-04-27T10:00:00', 'test')
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Фактов Федор Иванович"}).status_code)
            blocked = client.post(
                "/employee/request/actual",
                data={
                    "employee_key": "фактов федор иванович",
                    "response_id": "905",
                    "actual_work_date": "2026-04-22",
                    "actual_work_time": "10:00 - 12:00",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, blocked.status_code)
        self.assertIn("ввод фактически", unquote(blocked.headers["location"]).lower())


    def test_archiving_user_cancels_only_unlocked_active_requests(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=907,
            full_name="Архивов Иван Иванович",
            full_name_key="архивов иван иванович",
            planned_date="2026-04-22",
        )
        insert_planned_request(
            self.db_path,
            response_id=908,
            full_name="Архивов Иван Иванович",
            full_name_key="архивов иван иванович",
            planned_date="2026-04-25",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', '2026-04-25', '2026-04-26', 'root', '2026-04-20T10:00:00', 'locked')
                """
            )
            conn.commit()

        with patch.dict("os.environ", SUPERUSER_ENV), patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(303, login_superuser(client).status_code)
            response = client.post(
                "/admin/employee/status",
                data={"employee_key": "архивов иван иванович", "employee_status": "archived", "status_reason": "left"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as conn:
            unlocked_status = conn.execute(
                "SELECT status FROM app_request_state WHERE response_id = 907",
            ).fetchone()[0]
            locked_state = conn.execute(
                "SELECT status FROM app_request_state WHERE response_id = 908",
            ).fetchone()
            user_status = conn.execute(
                "SELECT employee_status FROM app_employee_profile WHERE full_name_key = ?",
                ("архивов иван иванович",),
            ).fetchone()[0]
        self.assertEqual("cancelled", unlocked_status)
        self.assertIsNone(locked_state)
        self.assertEqual("archived", user_status)

    def test_weekend_reports_use_db_grade_profile_preserve_web_facts_and_include_period_subtitle(self) -> None:
        report_date = "2026-04-22"
        report_range_start = "2026-04-20"
        report_range_end = "2026-04-26"
        planned_name = "Отчетов Олег Иванович"
        planned_key = "отчетов олег иванович"
        now = "2026-04-20T10:00:00"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_employee_profile (full_name_key, grade_12_plus, updated_at)
                VALUES (?, 1, ?)
                """,
                (planned_key, now),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, payment_type, task_description, justification,
                    planned_work_date, planned_work_time, target_work_date, source_file
                ) VALUES (
                    2001, 2001, ?, ?, ?, ?, 'Подать заявку', 'Двойная оплата', 'grade-row', 'reason',
                    ?, '10:00 - 15:30', ?, 'manual:grade'
                )
                """,
                (now, planned_name, planned_name, planned_key, report_date, report_date),
            )
            conn.execute(
                """
                INSERT INTO response_systems (response_id, system_order, system_name)
                VALUES (2001, 1, 'Система A')
                """
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, payment_type, task_description, justification,
                    planned_work_date, planned_work_time, target_work_date, source_file
                ) VALUES (
                    2002, 2002, ?, ?, ?, ?, 'Подать заявку', 'Отгул', 'fact-a', 'reason-a',
                    ?, '09:00 - 10:00', ?, 'manual:fact-a'
                )
                """,
                (now, planned_name, planned_name, planned_key, report_date, report_date),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, payment_type, task_description, justification,
                    planned_work_date, planned_work_time, target_work_date, source_file
                ) VALUES (
                    2003, 2003, ?, ?, ?, ?, 'Подать заявку', 'Отгул', 'fact-b', 'reason-b',
                    ?, '09:00 - 10:00', ?, 'manual:fact-b'
                )
                """,
                (now, planned_name, planned_name, planned_key, report_date, report_date),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date, actual_work_time, created_at, updated_at
                ) VALUES ('req:2002', 2002, ?, 'in_fact', ?, '09:00 - 10:00', ?, ?)
                """,
                (planned_key, report_date, now, now),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date, actual_work_time, created_at, updated_at
                ) VALUES ('req:2003', 2003, ?, 'in_fact', ?, '10:00 - 11:00', ?, ?)
                """,
                (planned_key, report_date, now, now),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    3001, 3001, ?, ?, ?, ?, 'Указать отработанное время', ?, '08:00 - 09:00', 'manual:web-fact-1'
                )
                """,
                (now, planned_name, planned_name, planned_key, report_date),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    3002, 3002, ?, ?, ?, ?, 'Указать отработанное время', ?, '10:00 - 11:00', 'manual:web-fact-2'
                )
                """,
                (now, planned_name, planned_name, planned_key, report_date),
            )
            conn.commit()

        output_path = Path(self.tmpdir.name) / "weekly_reports.xlsx"
        env = dict(os.environ)
        env["PYTHONPATH"] = f"src{os.pathsep}{env.get('PYTHONPATH', '')}" if env.get("PYTHONPATH") else "src"
        completed = subprocess.run(
            [
                sys.executable,
                "src/build_weekend_reports.py",
                "--db",
                str(self.db_path),
                "--date-from",
                report_range_start,
                "--date-to",
                report_range_end,
                "--employees-csv",
                str(Path(self.tmpdir.name) / "missing.csv"),
                "--output",
                str(output_path),
            ],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, msg=(completed.stderr or completed.stdout))

        with zipfile.ZipFile(output_path) as zip_file:
            sheet_a2_values = {sheet_name: xlsx_sheet_cells(zip_file, sheet_name)["A2"] for sheet_name in ("Отчет 1", "Отчет 2", "Отчет 3", "Отчет 4")}
            report1_rows = xlsx_sheet_rows(zip_file, "Отчет 1")
            report3_rows = xlsx_sheet_rows(zip_file, "Отчет 3")
            report4_rows = xlsx_sheet_rows(zip_file, "Отчет 4")

        subtitle_prefix = "Период: 20.04.2026–26.04.2026; Дата подготовки: "
        for sheet_name in ("Отчет 1", "Отчет 2", "Отчет 3", "Отчет 4"):
            self.assertTrue(sheet_a2_values[sheet_name].startswith(subtitle_prefix))

        report1_header_row = report1_rows[4]
        report1_column_map = {value: column for column, value in report1_header_row.items() if value}
        grade_row = next(
            row
            for row in report1_rows.values()
            if row.get(report1_column_map["Перечень задач"]) == "grade-row"
        )
        self.assertEqual("Двойная оплата (грейд 12+)", grade_row[report1_column_map["Условия выхода"]])
        self.assertEqual("Из рабочего времени будет вычтен 1 час на обед", grade_row[report1_column_map["Комментарий"]])

        report3_data_rows = [row for row_index, row in report3_rows.items() if row_index >= 5 and row]
        self.assertEqual(2, len(report3_data_rows))
        self.assertEqual(["09:00 - 10:00", "10:00 - 11:00"], [row["C"] for row in report3_data_rows])

        report4_header_row = report4_rows[4]
        report4_column_map = {value: column for column, value in report4_header_row.items() if value}
        report4_data_rows = [row for row_index, row in report4_rows.items() if row_index >= 5 and row]
        self.assertEqual(3, len(report4_data_rows))
        self.assertEqual(
            "09:00 - 10:00",
            next(
                row[report4_column_map["Фактически отработанное время"]]
                for row in report4_data_rows
                if row[report4_column_map["Перечень задач"]] == "fact-a"
            ),
        )
        self.assertEqual(
            "10:00 - 11:00",
            next(
                row[report4_column_map["Фактически отработанное время"]]
                for row in report4_data_rows
                if row[report4_column_map["Перечень задач"]] == "fact-b"
            ),
        )

    def test_weekend_reports_keep_legacy_actuals_when_no_web_state_exists(self) -> None:
        report_date = "2026-04-24"
        report_range_start = "2026-04-20"
        report_range_end = "2026-04-26"
        legacy_name = "Легасев Лев Иванович"
        legacy_key = "легасев лев иванович"
        now = "2026-04-20T10:00:00"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    3003, 3003, ?, ?, ?, ?, 'Указать отработанное время', ?, '11:00 - 12:00', 'manual:legacy-only'
                )
                """,
                (now, legacy_name, legacy_name, legacy_key, report_date),
            )
            conn.commit()

        report_df = importlib.import_module("src.report_third_closure").build_report_dataframe(
            str(self.db_path),
            date_from=web_ui.date(2026, 4, 20),
            date_to=web_ui.date(2026, 4, 26),
        )

        self.assertEqual(1, len(report_df))
        self.assertEqual("Легасев Лев Иванович", report_df.iloc[0]["ФИО"])
        self.assertEqual("11:00 - 12:00", report_df.iloc[0]["Фактически отработанное время"])

    def test_report_three_keeps_legacy_for_cancelled_web_state_and_dedupes_non_cancelled_web_fact(self) -> None:
        report_third_closure = importlib.import_module("src.report_third_closure")
        report_date = "2026-08-04"
        now = "2026-07-23T10:00:00"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    4201, 4201, ?, ?, ?, ?, 'Указать отработанное время', ?, '08:00 - 09:00', 'manual:legacy-cancelled'
                )
                """,
                (now, "Отмененов Олег Иванович", "Отмененов Олег Иванович", "отмененов олег иванович", report_date),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date, actual_work_time, created_at, updated_at
                ) VALUES ('req:4201', 4201, 'отмененов олег иванович', 'cancelled', ?, '09:00 - 10:00', ?, ?)
                """,
                (report_date, now, now),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    4202, 4202, ?, ?, ?, ?, 'Указать отработанное время', ?, '12:00 - 13:00', 'manual:legacy-web'
                )
                """,
                (now, "Неотменов Николай Иванович", "Неотменов Николай Иванович", "неотменов николай иванович", report_date),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    4203, 4203, ?, ?, ?, ?, 'Подать заявку', ?, '10:00 - 11:00', 'manual:web-fact'
                )
                """,
                (now, "Неотменов Николай Иванович", "Неотменов Николай Иванович", "неотменов николай иванович", report_date),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, actual_work_date, actual_work_time, created_at, updated_at
                ) VALUES ('req:4203', 4203, 'неотменов николай иванович', 'in_fact', ?, '10:00 - 11:00', ?, ?)
                """,
                (report_date, now, now),
            )
            conn.commit()

        report_df = report_third_closure.build_report_dataframe(
            str(self.db_path),
            date_from=web_ui.date(2026, 8, 4),
            date_to=web_ui.date(2026, 8, 4),
        )

        self.assertEqual(2, len(report_df))
        self.assertEqual(
            {
                ("Отмененов Олег Иванович", "04.08.2026", "08:00 - 09:00"),
                ("Неотменов Николай Иванович", "04.08.2026", "10:00 - 11:00"),
            },
            {
                (row["ФИО"], row["Дата фактического выхода"], row["Фактически отработанное время"])
                for _, row in report_df.iterrows()
            },
        )

    def test_report_four_maps_single_legacy_fact_to_active_request_and_marks_cancelled_not_required(self) -> None:
        report_four_reconciliation = importlib.import_module("src.report_four_reconciliation")
        report_date = "2026-08-12"
        now = "2026-07-23T10:00:00"

        insert_planned_request(
            self.db_path,
            response_id=4301,
            full_name="Свереванов Степан Иванович",
            full_name_key="свереванов степан иванович",
            planned_date=report_date,
        )
        insert_planned_request(
            self.db_path,
            response_id=4302,
            full_name="Свереванов Степан Иванович",
            full_name_key="свереванов степан иванович",
            planned_date=report_date,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:4301', 4301, 'свереванов степан иванович', 'active', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:4302', 4302, 'свереванов степан иванович', 'cancelled', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO survey_responses (
                    response_id, source_row, start_time, full_name, full_name_normalized, full_name_key,
                    request_type, actual_work_date, actual_work_time, source_file
                ) VALUES (
                    5301, 5301, ?, ?, ?, ?, 'Указать отработанное время', ?, '08:00 - 09:00', 'manual:report-4-legacy'
                )
                """,
                (now, "Свереванов Степан Иванович", "Свереванов Степан Иванович", "свереванов степан иванович", report_date),
            )
            conn.commit()

        report_df = report_four_reconciliation.build_report_dataframe(
            str(self.db_path),
            date_from=web_ui.date(2026, 8, 12),
            date_to=web_ui.date(2026, 8, 12),
        )

        self.assertEqual(2, len(report_df))
        active_row = report_df.loc[report_df["Статус заявки"] == "Заявка подана"].iloc[0]
        cancelled_row = report_df.loc[report_df["Статус заявки"] == "Отменена"].iloc[0]
        self.assertEqual("Да", active_row["Предоставил фактически отработанное время"])
        self.assertEqual("12.08.2026", active_row["Дата фактического выхода"])
        self.assertEqual("08:00 - 09:00", active_row["Фактически отработанное время"])
        self.assertEqual("Не требуется", cancelled_row["Предоставил фактически отработанное время"])
        self.assertTrue(cancelled_row["Дата фактического выхода"] != cancelled_row["Дата фактического выхода"])
        self.assertTrue(cancelled_row["Фактически отработанное время"] != cancelled_row["Фактически отработанное время"])

    def test_employee_cancel_is_blocked_by_effective_override_date_inside_planning_lock(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=4301,
            full_name="Локов Леонид Иванович",
            full_name_key="локов леонид иванович",
            planned_date="2026-08-03",
        )
        with sqlite3.connect(self.db_path) as conn:
            now = "2026-07-23T10:00:00"
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, override_planned_work_date, created_at, updated_at
                ) VALUES ('req:4301', 4301, 'локов леонид иванович', 'active', '2026-08-12', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('planning', '2026-08-10', '2026-08-16', 'root', ?, 'override lock')
                """,
                (now,),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Локов Леонид Иванович"}).status_code)
            blocked = client.post(
                "/employee/request/cancel",
                data={
                    "employee_key": "локов леонид иванович",
                    "response_id": "4301",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, blocked.status_code)
        self.assertIn("отмена доступна только администратору", unquote(blocked.headers["location"]).lower())
        with sqlite3.connect(self.db_path) as conn:
            state = conn.execute(
                "SELECT status, override_planned_work_date FROM app_request_state WHERE response_id = 4301"
            ).fetchone()
        self.assertEqual(("active", "2026-08-12"), state)

    def test_admin_requests_overview_uses_planned_date_for_actual_lock_when_actual_date_is_missing(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=4303,
            full_name="Локов Лев Иванович",
            full_name_key="локов лев иванович",
            planned_date="2026-08-13",
        )
        with sqlite3.connect(self.db_path) as conn:
            now = "2026-07-23T10:00:00"
            conn.execute(
                """
                INSERT INTO app_request_state (
                    request_uid, response_id, full_name_key, status, created_at, updated_at
                ) VALUES ('req:4303', 4303, 'локов лев иванович', 'in_progress', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO app_period_lock (lock_type, date_from, date_to, created_by, created_at, comment)
                VALUES ('actual', '2026-08-13', '2026-08-19', 'root', ?, 'actual lock')
                """,
                (now,),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            overview = web_ui.get_admin_requests_overview()

        item = next(row for row in overview if row["response_id"] == 4303)
        self.assertEqual("", item["actual_work_date_ru"])
        self.assertEqual("13.08.2026 - 19.08.2026", item["actual_lock_label"])

    def test_blocked_employee_cannot_login(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=906,
            full_name="Блоков Борис Иванович",
            full_name_key="блоков борис иванович",
            planned_date="2026-04-22",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_employee_profile (full_name_key, grade_12_plus, employee_status, updated_at)
                VALUES (?, 0, 'blocked', '2026-04-20T10:00:00')
                """,
                ("блоков борис иванович",),
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            response = client.post(
                "/employee/login",
                data={"full_name": "Блоков Борис Иванович"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertIn("заблокирован", unquote(response.headers["location"]).lower())


if __name__ == "__main__":
    unittest.main()
