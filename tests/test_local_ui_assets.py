"""Check the corporate bundle and the role-aware shared navigation."""
import hashlib
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from src import team_directory as directory, web_ui as ui


class PageResources(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.resources = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') in ('stylesheet', 'icon'):
            self.resources.add(attrs['href'])
        elif tag in ('script', 'img', 'iframe') and attrs.get('src'):
            self.resources.add(attrs['src'])
        elif tag == 'use':
            self.resources.add(attrs['href'].split('#')[0])


class LocalInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_patch = patch.object(ui, 'DB_PATH', Path(self.temp.name) / 'test.sqlite')
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
        with ui.get_db_connection() as conn:
            directory.apply_import(conn, directory.plan_import(conn, [
                ['Руководитель Тестовый', 'manager@example.org', ''],
                ['Сотрудник Тестовый', 'worker@example.org', 'manager@example.org'],
            ]))
            self.keys = {p['email']: p['key'] for p in directory.employees(conn).values()}
            for email, key in self.keys.items():
                ui.upsert_employee_token(conn, key, email)

    def test_all_cabinets_serve_complete_local_asset_bundle(self):
        self.client.post('/superuser/login', data={
            'superuser_login': 'root', 'superuser_password': 'test-password'})
        resources = set()
        for path in ('/', '/employee', '/admin', '/admin/requests', '/admin/users',
                     '/admin/roster', '/admin/test-data', '/manager', '/employee?admin_mode=1'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                found = PageResources(response.text).resources
                self.assertTrue(any('tabler.min.css' in item for item in found))
                self.assertTrue(any('tabler.min.js' in item for item in found))
                self.assertTrue(any('icons.svg' in item for item in found))
                resources.update(found)
        for url in resources:
            with self.subTest(resource=url):
                parts = urlsplit(url)
                self.assertFalse(parts.scheme or parts.netloc)
                self.assertTrue(parts.path.startswith('/static/'))
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('text/html', response.headers['content-type'])
                local_path = ui.STATIC_DIR / parts.path.removeprefix('/static/')
                self.assertEqual(response.content, local_path.read_bytes())
                self.assertIn(hashlib.sha256(response.content).hexdigest()[:12], parts.query)
                if parts.path.endswith('.css'):
                    self.assertNotIn('@import', response.text)
                    for dependency in re.findall(r'url\(([^)]+)\)', response.text):
                        self.assertTrue(dependency.strip(' "\'').startswith('data:'), dependency)

    def test_vendored_assets_match_recorded_integrity(self):
        vendor = ui.STATIC_DIR / 'vendor' / 'tabler-1.5.1'
        manifest = json.loads((vendor / 'manifest.json').read_text())
        for name, digest in manifest['sha256'].items():
            self.assertEqual(hashlib.sha256((vendor / name).read_bytes()).hexdigest(), digest)
        self.assertIn('MIT', (vendor / 'LICENSE').read_text())

    def test_navigation_rechecks_current_role_and_revoked_access(self):
        def navigation():
            html = self.client.get('/').text
            return html.split('<aside', 1)[1].split('</aside>', 1)[0]

        self.assertNotIn('href="/manager"', navigation())
        self.assertNotIn('href="/admin"', navigation())
        self.client.cookies.set(ui.EMPLOYEE_TOKEN_COOKIE_NAME, 'worker@example.org')
        self.assertNotIn('href="/manager"', navigation())
        self.client.cookies.set(ui.EMPLOYEE_TOKEN_COOKIE_NAME, 'manager@example.org')
        self.assertIn('href="/manager"', navigation())
        self.assertNotIn('href="/admin"', navigation())
        with ui.get_db_connection() as conn:
            ui.update_employee_admin_role(conn, self.keys['manager@example.org'], True, 'test')
        self.assertIn('href="/admin"', navigation())
        with ui.get_db_connection() as conn:
            ui.update_employee_status(conn, self.keys['manager@example.org'], 'blocked', 'test', 'test')
        self.assertNotIn('href="/manager"', navigation())
        self.assertNotIn('href="/admin"', navigation())

    def test_static_mount_does_not_expose_application_files(self):
        for path in ('/static/.env', '/static/survey_results.db', '/static/%2e%2e/src/web_ui.py'):
            self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == '__main__':
    unittest.main()
