import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "deploy/scripts"
spec = importlib.util.spec_from_file_location("restore_points", SCRIPTS / "create-restore-point.py")
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class RestorePointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "application with spaces"
        self.app.mkdir()
        (self.app / "src").mkdir()
        (self.app / "src/web_ui.py").write_text('VERSION = "previous"\n')
        (self.app / "requirements.txt").write_text("previous-dependency==1.0\n")
        (self.app / "venv/bin").mkdir(parents=True)
        (self.app / "venv/bin/python").symlink_to(sys.executable)
        (self.app / "venv/installed-version.txt").write_text("previous environment")
        (self.app / "reports").mkdir()
        (self.app / "reports/not-source.xlsx").write_bytes(b"not source")
        self.db = self.root / "db.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY, text TEXT)")
            conn.execute("INSERT INTO requests VALUES (1, ?)", ('Старые данные — =пример',))
        self.env = self.root / "config/application.env"
        self.env.parent.mkdir()
        self.env.write_text("WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=fake-test-password\n")
        self.backups = self.root / "backups"
        runtime = self.root / ".local/bin"
        runtime.mkdir(parents=True)
        for name in ("start", "watchdog"):
            file = runtime / ("test-service-" + name + ".sh")
            file.write_text("#!/bin/sh\nexit 0\n")
            file.chmod(0o700)
        home_patch = patch.object(restore.Path, "home", return_value=self.root)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        original_run = subprocess.run
        def run(command, **kwargs):
            if command == ["crontab", "-l"]:
                return subprocess.CompletedProcess(command, 0, "# another application\n* * * * * example\n", "")
            return original_run(command, **kwargs)
        run_patch = patch.object(restore.subprocess, "run", side_effect=run)
        run_patch.start()
        self.addCleanup(run_patch.stop)

    def create(self):
        return restore.create_restore_point(self.app, self.db, self.env, self.backups,
                                            "test-instance", "test-service", self.root / "state", "0.0.0.0", 8081)

    def test_full_snapshot_can_restore_previous_code_environment_config_and_database(self):
        point = self.create()
        manifest = restore.verify_restore_point(point)
        self.assertEqual(manifest["app_dir"], str(self.app))
        self.assertEqual(manifest["db_path"], str(self.db))
        self.assertEqual(manifest["service"], "test-service")
        self.assertEqual(manifest["port"], 8081)
        self.assertEqual(set(manifest["runtime_files"]), {"start.sh", "watchdog.sh"})
        self.assertEqual(point.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in point.iterdir()))
        # Simulate later changes, then reconstruct the old version in a separate directory.
        (self.app / "src/web_ui.py").write_text('VERSION = "new"\n')
        (self.app / "venv/installed-version.txt").write_text("new environment")
        self.env.write_text("new configuration")
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO requests VALUES (2, 'new record')")
        recovered = self.root / "recovered"
        recovered.mkdir()
        with tarfile.open(point / "application.tar.gz") as archive:
            self.assertNotIn("application/reports", archive.getnames())
            # The fixture contains a known interpreter symlink, just like a venv.
            archive.extractall(recovered, filter="fully_trusted")
        self.assertEqual((recovered / "application/src/web_ui.py").read_text(), 'VERSION = "previous"\n')
        self.assertEqual((recovered / "application/venv/installed-version.txt").read_text(), "previous environment")
        self.assertEqual((point / "application.env").read_text(), "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=fake-test-password\n")
        with sqlite3.connect(point / "database.sqlite") as conn:
            self.assertEqual(conn.execute("SELECT * FROM requests").fetchall(), [(1, 'Старые данные — =пример')])
        with sqlite3.connect(recovered / "from-dump.sqlite") as conn:
            conn.executescript((point / "database.sql").read_text())
            self.assertEqual(conn.execute("SELECT * FROM requests").fetchall(), [(1, 'Старые данные — =пример')])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)

    def test_corruption_and_missing_configuration_are_detected(self):
        point = self.create()
        (point / "application.env").write_text("damaged")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            restore.verify_restore_point(point)
        self.env.unlink()
        with self.assertRaisesRegex(RuntimeError, "environment file"):
            self.create()

    def test_failed_backup_never_publishes_an_incomplete_point_or_changes_live_data(self):
        original = self.db.read_bytes()
        with patch.object(restore.tarfile, "open", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.create()
        self.assertEqual(list((self.backups / "restore-points").iterdir()), [])
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual((self.app / "src/web_ui.py").read_text(), 'VERSION = "previous"\n')

    def test_repeated_points_preserve_history_and_nested_backups_are_not_archived(self):
        self.backups = self.app / "custom-backups"
        first, second = self.create(), self.create()
        self.assertNotEqual(first, second)
        restore.verify_restore_point(first)
        with tarfile.open(second / "application.tar.gz") as archive:
            self.assertFalse(any(name.startswith("application/custom-backups") for name in archive.getnames()))

    def test_unreadable_crontab_aborts_backup(self):
        with patch.object(restore.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaisesRegex(RuntimeError, "Cannot save existing crontab"):
                self.create()
        self.assertEqual(list((self.backups / "restore-points").iterdir()), [])

    def test_missing_startup_script_aborts_backup(self):
        (self.root / ".local/bin/test-service-start.sh").unlink()
        with self.assertRaisesRegex(RuntimeError, "startup script not found"):
            self.create()
        self.assertEqual(list((self.backups / "restore-points").iterdir()), [])


class UpdateBackupOrderTests(unittest.TestCase):
    def test_failed_backup_prevents_sync_and_success_precedes_sync_for_both_profiles(self):
        for production in (False, True):
            for fail in (False, True):
                with self.subTest(production=production, fail=fail), tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary)
                    binaries = folder / "bin"
                    binaries.mkdir()
                    log = folder / "commands.jsonl"
                    shim = f'''#!{Path(sys.executable).resolve()}
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
payload = sys.stdin.read() if tool == 'ssh' and 'python3 - ' in sys.argv[-1] else ''
with open(os.environ['TEST_COMMAND_LOG'], 'a') as stream:
    stream.write(json.dumps(dict(tool=tool, args=sys.argv[1:], payload=payload)) + '\\n')
if payload and os.environ['TEST_BACKUP_FAIL'] == '1':
    sys.exit(23)
'''
                    for tool in ("ssh", "rsync"):
                        file = binaries / tool
                        file.write_text(shim)
                        file.chmod(0o700)
                    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                           "TEST_COMMAND_LOG": str(log), "TEST_BACKUP_FAIL": "1" if fail else "0"}
                    script = "update-production-corporate-server.sh" if production else "update-corporate-server.sh"
                    result = subprocess.run(["bash", str(SCRIPTS / script)], env=env, capture_output=True, text=True)
                    self.assertTrue(log.exists(), result.stderr)
                    commands = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual(commands[0]["tool"], "ssh")
                    self.assertEqual(commands[0]["payload"], (SCRIPTS / "create-restore-point.py").read_text())
                    backup_command = commands[0]["args"][-1]
                    self.assertIn("--env-file", backup_command)
                    self.assertIn("--app-dir", backup_command)
                    self.assertIn("work-on-holiday-production" if production else "apps/work-on-holiday'", backup_command)
                    if fail:
                        self.assertEqual(result.returncode, 23)
                        self.assertEqual(len(commands), 1)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(commands[2]["tool"], "rsync")


if __name__ == "__main__":
    unittest.main()
