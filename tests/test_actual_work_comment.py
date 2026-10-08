from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from src import web_ui as ui
from src.app_request_state import ensure_app_tables

ADMIN_ENV = {'WORK_ON_HOLIDAY_SUPERUSER_LOGIN': 'root', 'WORK_ON_HOLIDAY_SUPERUSER_PASSWORD': 'test-password'}


class ActualWorkCommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'comment.sqlite'
        self.db_patch = patch.object(ui, 'DB_PATH', self.path)
        self.db_patch.start()
        self.env_patch = patch.dict('os.environ', ADMIN_ENV)
        self.env_patch.start()
        ui.ensure_app_tables_for_app()
        with ui.get_db_connection() as conn:
            self.key = ui.register_employee_directory_entry(conn, 'Тестов Иван Иванович')['employee_key']
            self.manager = ui.register_employee_directory_entry(conn, 'Начальник Пётр Иванович')['employee_key']
            ui.upsert_employee_token(conn, self.key, 'employee-token')
            ui.upsert_employee_token(conn, self.manager, 'manager-token')
            conn.execute('INSERT INTO app_org_employee VALUES (?,?,NULL)', (self.manager, 'manager@example.org'))
            conn.execute('INSERT INTO app_org_employee VALUES (?,?,?)', (self.key, 'employee@example.org', self.manager))
            conn.execute('''INSERT INTO survey_responses(response_id,full_name_key,full_name,request_type,planned_work_date,planned_work_time)
                            VALUES (1,?,?,'Подать заявку','2026-10-03','09:00 - 18:00')''', (self.key, 'Тестов Иван Иванович'))
            ui.upsert_request_state(conn, request_uid='req:1', response_id=1, full_name_key=self.key,
                                    updates={'status':'in_progress'})
        self.client = TestClient(ui.app)
        self.client.cookies.set(ui.EMPLOYEE_TOKEN_COOKIE_NAME, 'employee-token')
        self.payload = dict(employee_key=self.key, response_id='1', actual_work_date='2026-10-03',
                            actual_work_time='10:00 - 12:00', actual_work_comment='Проверены отчёты\nОшибок нет')

    def tearDown(self):
        self.client.close()
        self.db_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def save(self, **changes):
        return self.client.post('/employee/request/actual', data={**self.payload, **changes}, follow_redirects=False)

    def state(self):
        with ui.get_db_connection() as conn:
            return ui.get_request_state(conn, 'req:1')

    def admin_client(self):
        client = TestClient(ui.app)
        client.post('/superuser/login', data={'superuser_login':'root', 'superuser_password':'test-password'})
        return client

    def textarea(self, html):
        return re.search(r'<textarea[^>]*name="actual_work_comment"[^>]*>.*?</textarea>', html, re.S)[0]

    def test_initial_save_edit_clear_and_old_client_omission(self):
        self.assertEqual(self.save().status_code, 303)
        self.assertEqual(self.state()['status'], 'in_fact')
        self.assertEqual(self.state()['actual_work_comment'], self.payload['actual_work_comment'])
        self.save(actual_work_comment='Исправленный комментарий')
        legacy = dict(self.payload)
        del legacy['actual_work_comment']
        self.client.post('/employee/request/actual', data=legacy)
        self.assertEqual(self.state()['actual_work_comment'], 'Исправленный комментарий')
        self.save(actual_work_comment='')
        self.assertEqual(self.state()['actual_work_comment'], '')

    def test_missing_or_invalid_time_cannot_write_comment(self):
        before = self.state()
        for value in ['', ' ', '10:00', '25:00 - 26:00']:
            with self.subTest(time=value):
                self.save(actual_work_time=value)
                self.assertEqual(self.state(), before)
        missing = dict(self.payload)
        del missing['actual_work_time']
        self.client.post('/employee/request/actual', data=missing)
        self.assertEqual(self.state(), before)
        self.save()
        saved = self.state()
        self.save(actual_work_time='', actual_work_comment='Потерянный текст')
        self.assertEqual(self.state(), saved)

    def test_closure_preserves_comment_and_blocks_stale_form_even_for_admin(self):
        self.save()
        with self.admin_client() as admin:
            admin.post('/admin/request/status', data={'employee_key':self.key, 'response_id':1, 'status':'completed'})
            closed = self.state()
            self.assertEqual(closed['actual_work_comment'], self.payload['actual_work_comment'])
            self.save(actual_work_comment='Изменение после закрытия')
            self.assertEqual(self.state(), closed)
            admin.post('/employee/request/actual', data={**self.payload, 'admin_mode':'1', 'actual_work_comment':'Обход'})
            self.assertEqual(self.state(), closed)
            page = admin.get('/employee', params={'employee_key':self.key, 'admin_mode':1})
            self.assertIn('readonly', self.textarea(page.text))
            self.assertIn('Заявка закрыта', page.text)
            admin.post('/admin/request/status', data={'employee_key':self.key, 'response_id':1, 'status':'in_fact'})
        self.save(actual_work_comment='После явного открытия')
        self.assertEqual(self.state(), closed)  # Closed is terminal in the current workflow.

    def test_cancelled_and_not_accepted_requests_deny_writes(self):
        for status in ['active', 'cancelled']:
            with self.subTest(status=status):
                with ui.get_db_connection() as conn:
                    ui.upsert_request_state(conn, request_uid='req:1', response_id=1, full_name_key=self.key,
                                            updates={'status':status})
                before = self.state()
                self.save()
                self.assertEqual(self.state(), before)

    def test_period_closure_and_locks_preserve_comment(self):
        self.save()
        with self.admin_client() as admin:
            admin.post('/admin/locks/create', data={'lock_type':'actual','date_from':'2026-09-28','date_to':'2026-10-04'})
            self.assertEqual(self.state()['status'], 'completed')
            self.assertEqual(self.state()['actual_work_comment'], self.payload['actual_work_comment'])
        # Simulate a legacy in-fact row in a locked period to verify that a
        # forged date cannot bypass the lock on the effective planned date.
        with ui.get_db_connection() as conn:
            ui.upsert_request_state(conn, request_uid='req:1', response_id=1, full_name_key=self.key,
                                    updates={'status':'in_fact'})
        before = self.state()
        self.save(actual_work_date='2026-10-05', actual_work_comment='Обход даты')
        self.assertEqual(self.state(), before)
        with ui.get_db_connection() as conn:
            conn.execute('DELETE FROM app_period_lock')
            conn.execute("INSERT INTO app_report_lock VALUES (1,'2026-09-28','2026-10-04','report.xlsx',CURRENT_TIMESTAMP)")
        self.save(actual_work_comment='Обход блокировки отчётом')
        self.assertEqual(self.state(), before)

    def test_manager_can_read_but_cannot_change_comment(self):
        self.save(actual_work_comment='<script>alert(1)</script>\nГотово')
        self.client.cookies.set(ui.EMPLOYEE_TOKEN_COOKIE_NAME, 'manager-token')
        page = self.client.get('/manager/requests/1')
        self.assertEqual(page.status_code, 200)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertNotIn('<script>alert(1)</script>', page.text)
        before = self.state()
        self.save(actual_work_comment='Чужая правка', admin_mode='1')
        self.assertEqual(self.state(), before)
        with self.admin_client() as admin:
            self.assertIn('&lt;script&gt;', admin.get('/admin/requests').text)

    def test_empty_fact_starts_readonly_and_saved_fact_is_editable(self):
        page = self.client.get('/employee').text
        self.assertIn('readonly', self.textarea(page))
        self.assertRegex(page, r'name="actual_work_time"[^>]*value=""')
        self.save()
        page = self.client.get('/employee').text
        self.assertNotIn('readonly', self.textarea(page))
        self.assertIn(self.payload['actual_work_comment'], self.textarea(page))
        with ui.get_db_connection() as conn:
            ui.upsert_request_state(conn, request_uid='req:1', response_id=1, full_name_key=self.key,
                                    updates={'status':'in_progress', 'actual_work_time':None})
        page = self.client.get('/employee').text
        self.assertIn('readonly', self.textarea(page))
        self.assertIn(self.payload['actual_work_comment'], self.textarea(page))

    def test_comment_migration_is_additive_and_repeatable(self):
        with ui.get_db_connection() as conn:
            conn.execute('ALTER TABLE app_request_state DROP COLUMN actual_work_comment')
            ensure_app_tables(conn)
            ensure_app_tables(conn)
            self.assertIsNone(ui.get_request_state(conn, 'req:1')['actual_work_comment'])
            self.assertEqual(ui.get_request_state(conn, 'req:1')['status'], 'in_progress')
        self.save()
        with ui.get_db_connection() as conn:
            ensure_app_tables(conn)
            self.assertEqual(ui.get_request_state(conn, 'req:1')['actual_work_comment'], self.payload['actual_work_comment'])
