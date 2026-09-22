import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import team_directory as d, web_ui as ui


class RegistrationRosterEmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite'
        db_patch = patch.object(ui, 'DB_PATH', self.path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        env_patch = patch.dict('os.environ', {
            'WORK_ON_HOLIDAY_SUPERUSER_LOGIN': 'root',
            'WORK_ON_HOLIDAY_SUPERUSER_PASSWORD': 'test-password',
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        ui.ensure_app_tables_for_app()
        self.client = TestClient(ui.app)
        self.addCleanup(self.client.close)

    def legacy(self, key, name, email=None):
        with ui.get_db_connection() as conn:
            conn.execute('''INSERT INTO app_employee_directory
                (full_name_key,full_name,work_email,created_at,updated_at)
                VALUES (?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''', (key, name, email))

    def preview(self, rows):
        self.client.post('/superuser/login', data={
            'superuser_login': 'root', 'superuser_password': 'test-password'})
        content = (';'.join(d.HEADERS) + '\n' + '\n'.join(';'.join(row) for row in rows)).encode()
        response = self.client.post('/admin/roster/preview', files={'file': ('roster.csv', content)})
        self.assertEqual(response.status_code, 200)
        with ui.get_db_connection() as conn:
            identifier = conn.execute('SELECT id FROM app_roster_draft ORDER BY rowid DESC').fetchone()[0]
        return identifier, response

    def test_registration_requires_valid_email_without_creating_records(self):
        name = 'Новиков Роман Сергеевич'
        for address in ['', 'not-an-email', 'a b@example.org', 'x' * 250 + '@example.org']:
            with self.subTest(address=address):
                response = self.client.post('/employee/login', data={'full_name': name, 'work_email': address})
                self.assertIn('почт', response.text)
                self.assertNotIn('Токен сотрудника создан', response.text)
                with ui.get_db_connection() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM app_employee_directory').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM app_employee_auth').fetchone()[0], 0)

    def test_registration_stores_normalized_email_and_email_login_works(self):
        response = self.client.post('/employee/login', data={
            'full_name': 'Новиков Роман Сергеевич', 'work_email': '  NOVIKOV@Example.Org  ', 'grade_12_plus': '1'})
        self.assertIn('Токен сотрудника создан', response.text)
        token = self.client.cookies.get(ui.EMPLOYEE_TOKEN_COOKIE_NAME)
        self.assertTrue(token)
        with ui.get_db_connection() as conn:
            person = next(iter(d.employees(conn).values()))
            self.assertEqual(person['email'], 'novikov@example.org')
            self.assertEqual(ui.get_employee_profile(conn, person['key'])['grade_12_plus'], 1)
            self.assertIsNone(conn.execute('SELECT 1 FROM app_org_employee').fetchone())
        self.client.cookies.clear()
        result = self.client.post('/employee/login', data={
            'full_name': 'NOVIKOV@example.org', 'access_token': token}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.client.cookies.get(ui.EMPLOYEE_TOKEN_COOKIE_NAME), token)

    def test_duplicate_email_rejected_for_directory_and_imported_people(self):
        self.legacy('existing', 'Старый Сотрудник', 'existing@example.org')
        with ui.get_db_connection() as conn:
            d.apply_import(conn, d.plan_import(conn, [['Другой Сотрудник', 'imported@example.org', '']]))
            before = d.fingerprint(conn)
        for address in [' EXISTING@Example.Org ', 'IMPORTED@example.org']:
            response = self.client.post('/employee/login', data={
                'full_name': 'Новиков Роман Сергеевич', 'work_email': address})
            self.assertIn('Рабочая почта уже используется', response.text)
        with ui.get_db_connection() as conn:
            self.assertEqual(d.fingerprint(conn), before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM app_employee_auth').fetchone()[0], 0)

    def test_registration_email_longer_than_name_limit_can_be_used_for_login(self):
        address = 'employee@' + '.'.join(['a' * 50] * 3) + '.org'
        response = self.client.post('/employee/login', data={
            'full_name': 'Длиннов Иван Иванович', 'work_email': address})
        self.assertIn('Токен сотрудника создан', response.text)
        token = self.client.cookies.get(ui.EMPLOYEE_TOKEN_COOKIE_NAME)
        self.client.cookies.clear()
        result = self.client.post('/employee/login', data={
            'full_name': address, 'access_token': token}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.client.cookies.get(ui.EMPLOYEE_TOKEN_COOKIE_NAME), token)

    def test_existing_login_cannot_change_email_from_registration_field(self):
        for key, address in [('legacy', None), ('known', 'known@example.org')]:
            name = 'Старый Сотрудник' if key == 'legacy' else 'Известный Сотрудник'
            self.legacy(key, name, address)
            with ui.get_db_connection() as conn:
                ui.upsert_employee_token(conn, key, key + '-token')
            response = self.client.post('/employee/login', data={
                'full_name': name, 'work_email': 'intruder@example.org', 'access_token': key + '-token'},
                follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            with ui.get_db_connection() as conn:
                self.assertEqual(d.employees(conn)[key]['email'], address or '')

    def test_unique_name_normalization_preserves_identity_profile_and_requests(self):
        self.legacy('stable-key', 'Ёлкин Иван Сергеевич')
        with ui.get_db_connection() as conn:
            ui.upsert_employee_token(conn, 'stable-key', 'secret')
            ui.update_employee_admin_role(conn, 'stable-key', True, 'test')
            ui.upsert_employee_grade_12_plus(conn, 'stable-key', True)
            conn.execute('''INSERT INTO survey_responses(response_id,full_name_key,full_name,request_type)
                            VALUES (1,'stable-key','Ёлкин Иван Сергеевич','Подать заявку')''')
            auth = dict(ui.get_employee_token_record(conn, 'stable-key'))
            profile = ui.get_employee_profile(conn, 'stable-key')
            plan = d.plan_import(conn, [['  елкин   ИВАН сергеевич ', 'ELKIN@example.org', '']])
            self.assertFalse(plan['errors'])
            self.assertFalse(plan['skipped'])
            self.assertEqual(plan['changes'][0]['key'], 'stable-key')
            d.apply_import(conn, plan)
            self.assertEqual(d.employees(conn)['stable-key']['email'], 'elkin@example.org')
            self.assertEqual(dict(ui.get_employee_token_record(conn, 'stable-key')), auth)
            self.assertEqual(ui.get_employee_profile(conn, 'stable-key'), profile)
            self.assertEqual(conn.execute('SELECT full_name_key FROM survey_responses WHERE response_id=1').fetchone()[0], 'stable-key')
            again = d.plan_import(conn, [['Ёлкин Иван Сергеевич', 'elkin@example.org', '']])
            self.assertEqual(again['changes'][0]['key'], 'stable-key')
            self.assertEqual(again['changes'][0]['matched_by'], 'Рабочая почта')

    def test_ambiguous_names_and_dependent_chain_skipped_other_rows_apply(self):
        for key in ['namesake-a', 'namesake-b']:
            self.legacy(key, 'Полный Тёзка')
        rows = [['Нижний Сотрудник', 'bottom@example.org', 'middle@example.org'],
                ['Средний Сотрудник', 'middle@example.org', 'boss@example.org'],
                ['Полный Тезка', 'boss@example.org', ''],
                ['Однозначный Сотрудник', 'ok@example.org', '']]
        identifier, preview = self.preview(rows)
        self.assertIn('Пропущено: 3', preview.text)
        self.assertIn('Неоднозначное ФИО', preview.text)
        self.assertIn('Руководитель пропущен', preview.text)
        self.assertIn('Применить записи (1)', preview.text)
        response = self.client.post(f'/admin/roster/{identifier}/apply')
        self.assertIn('Пропущено: 3', response.text)
        with ui.get_db_connection() as conn:
            people = d.employees(conn)
            self.assertEqual(len(people), 3)
            self.assertEqual(people['namesake-a']['email'], '')
            self.assertEqual(people['namesake-b']['email'], '')
            self.assertEqual({p['email'] for p in people.values()}, {'', 'ok@example.org'})

    def test_competing_file_rows_do_not_pick_first_legacy_match(self):
        self.legacy('legacy', 'Полный Тёзка')
        rows = [['Полный Тезка', 'first@example.org', ''], ['Полный ТЁЗКА', 'second@example.org', '']]
        with ui.get_db_connection() as conn:
            before = d.fingerprint(conn)
            for ordered_rows in [rows, rows[::-1]]:
                plan = d.plan_import(conn, ordered_rows)
                self.assertEqual(plan['changes'], [])
                self.assertEqual(len(plan['skipped']), 2)
                self.assertFalse(plan['errors'])
                d.apply_import(conn, plan)
                self.assertEqual(d.fingerprint(conn), before)
        identifier, response = self.preview(rows)
        self.assertIn('Нет записей для применения', response.text)
        self.assertNotIn('Применить записи (', response.text)
        self.assertEqual(self.client.post(f'/admin/roster/{identifier}/apply').status_code, 422)

    def test_email_match_takes_priority_and_existing_email_is_not_replaced(self):
        self.legacy('blank', 'Полный Тёзка')
        self.legacy('known', 'Полный Тёзка', 'known@example.org')
        with ui.get_db_connection() as conn:
            plan = d.plan_import(conn, [['Полный Тезка', 'KNOWN@example.org', ''],
                                        ['Полный Тезка', 'different@example.org', '']])
            self.assertEqual([p['key'] for p in plan['changes']], ['known'])
            self.assertEqual(len(plan['skipped']), 1)
            d.apply_import(conn, plan)
            self.assertEqual(d.employees(conn)['blank']['email'], '')
            self.assertEqual(d.employees(conn)['known']['email'], 'known@example.org')

    def test_skipped_existing_manager_remains_usable_by_email(self):
        self.legacy('boss-a', 'Полный Тёзка')
        self.legacy('boss-b', 'Полный Тёзка')
        self.legacy('middle', 'Средний Сотрудник', 'middle@example.org')
        with ui.get_db_connection() as conn:
            plan = d.plan_import(conn, [['Полный Тезка', 'boss@example.org', ''],
                                        ['Средний Сотрудник', 'middle@example.org', 'boss@example.org'],
                                        ['Нижний Сотрудник', 'bottom@example.org', 'middle@example.org']])
            self.assertFalse(plan['errors'])
            self.assertEqual(len(plan['skipped']), 2)
            d.apply_import(conn, plan)
            people = d.employees(conn)
            bottom = next(p for p in people.values() if p['email'] == 'bottom@example.org')
            self.assertEqual(bottom['manager'], 'middle')
            self.assertEqual(people['middle']['manager'], d.SUPERUSER_KEY)

    def test_directory_change_after_name_preview_requires_reupload(self):
        self.legacy('legacy', 'Полный Тёзка')
        identifier, _ = self.preview([['Полный Тезка', 'new@example.org', '']])
        self.legacy('another', 'Полный Тёзка')
        self.assertEqual(self.client.post(f'/admin/roster/{identifier}/apply').status_code, 409)
        with ui.get_db_connection() as conn:
            self.assertEqual(d.employees(conn)['legacy']['email'], '')

    def test_old_manual_binding_drafts_cannot_apply_after_policy_change(self):
        for bindings in [{}, {'new@example.org': '__new__'}]:
            identifier, _ = self.preview([['Новый Сотрудник', 'new@example.org', '']])
            with ui.get_db_connection() as conn:
                conn.execute('UPDATE app_roster_draft SET bindings=? WHERE id=?', (json.dumps(bindings), identifier))
            self.assertEqual(self.client.post(f'/admin/roster/{identifier}/apply').status_code, 409)
