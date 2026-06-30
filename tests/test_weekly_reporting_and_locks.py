from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.app_request_state import ensure_app_tables
from src import generate_users, init_db, report_first_management, report_second_requests, report_third_closure, web_ui


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


class WeeklyReportingAndLocksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.sqlite3"
        init_test_db(self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

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

    def test_actual_report_accepts_week_range_with_state_overrides(self) -> None:
        insert_planned_request(
            self.db_path,
            response_id=303,
            full_name="Сидоров Сидор Сидорович",
            full_name_key="сидоров сидор сидорович",
            planned_date="2026-04-22",
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
                    "planned_work_date": "2026-04-22",
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
        insert_planned_request(
            self.db_path,
            response_id=909,
            full_name="Новиков Павел Андреевич",
            full_name_key="новиков павел андреевич",
            planned_date="2026-04-26",
        )

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            login = client.post("/employee/login", data={"full_name": "Новиков Павел Андреевич"})
            self.assertEqual(200, login.status_code)

            create_response = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": "2026-04-27",
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
        self.assertIn("27.04.2026", cabinet.text)
        self.assertIn("понедельник", cabinet.text.lower())
        self.assertIn("19:00 - 22:00", cabinet.text)
        self.assertIn("Система B", cabinet.text)

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
        insert_planned_request(
            self.db_path,
            response_id=404,
            full_name="Тестов Тест Тестович",
            full_name_key="тестов тест тестович",
            planned_date="2026-04-22",
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
        self.assertIn('data-systems-editor', employee_response.text)
        self.assertIn("После ввода одной АС нажмите Enter или Tab", employee_response.text)
        self.assertIn('data-hamburger-menu="true"', employee_response.text)
        self.assertIn("<details", employee_response.text)
        self.assertIn("Создать новую заявку", employee_response.text)
        self.assertIn("Мои заявки", employee_response.text)
        self.assertIn("summary-row", employee_response.text)
        self.assertIn("22.04.2026", employee_response.text)
        self.assertIn("среда", employee_response.text.lower())
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
                    "planned_work_date": "2026-04-28",
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
        insert_planned_request(
            self.db_path,
            response_id=516,
            full_name="Грейдов Денис Сергеевич",
            full_name_key="грейдов денис сергеевич",
            planned_date="2026-04-22",
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
                    "planned_work_date": "2026-04-29",
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
        insert_planned_request(
            self.db_path,
            response_id=517,
            full_name="Оплатов Илья Сергеевич",
            full_name_key="оплатов илья сергеевич",
            planned_date="2026-04-22",
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
            self.assertIn('<option value="Двойная оплата">Двойная оплата</option>', cabinet.text)

            create_response = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": "2026-04-29",
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
            self.assertIn("перевыпуск токена", unquote(forgot.headers["location"]).lower())

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
        self.assertIn("Открыть кабинет", requests_response.text)
        self.assertIn('data-hamburger-menu="true"', requests_response.text)
        self.assertEqual(200, test_data_response.status_code)
        self.assertIn("Генерация тестовых данных", test_data_response.text)
        self.assertIn('data-date-picker="true"', test_data_response.text)
        self.assertIn('data-hamburger-menu="true"', test_data_response.text)

    def test_date_picker_keeps_popover_open_on_internal_clicks(self) -> None:
        for template_name in ("employee.html", "index.html", "admin_test_data.html"):
            template_text = (web_ui.TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn('popover.addEventListener("click"', template_text)
                self.assertIn("event.stopPropagation();", template_text)

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
                VALUES ('planning', '2026-04-20', '2026-04-26', 'root', '2026-04-20T10:00:00', 'test')
                """
            )
            conn.commit()

        with patch.object(web_ui, "DB_PATH", self.db_path):
            client = TestClient(web_ui.app)
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Локов Лев Иванович"}).status_code)
            blocked = client.post(
                "/employee/request/create",
                data={
                    "planned_work_date": "2026-04-25",
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
                    "planned_work_date": "2026-04-25",
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
                data={"employee_key": "возвратов виктор иванович", "response_id": "920", "status": "active"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
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
                    "planned_work_date": "2026-04-22",
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
        insert_planned_request(
            self.db_path,
            response_id=914,
            full_name="Закрытов Захар Иванович",
            full_name_key="закрытов захар иванович",
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
            self.assertEqual(200, client.post("/employee/login", data={"full_name": "Закрытов Захар Иванович"}).status_code)
            page = client.get("/employee")

        self.assertEqual(200, page.status_code)
        self.assertIn("Прием заявок за этот период закрыт", page.text)
        self.assertNotIn('action="/employee/request/correct"', page.text)
        self.assertIn('action="/employee/request/actual"', page.text)

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
                    "planned_work_date": "2026-04-30",
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
