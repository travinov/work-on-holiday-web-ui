from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "deploy" / "scripts"


class ProductionDeploymentProfileTests(unittest.TestCase):
    def test_shell_scripts_are_syntactically_valid(self) -> None:
        scripts = sorted(SCRIPTS_DIR.glob("*.sh"))
        self.assertTrue(scripts)
        for script in scripts:
            with self.subTest(script=script.name):
                subprocess.run(
                    ["bash", "-n", str(script)],
                    cwd=PROJECT_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_production_install_help_documents_isolated_defaults(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "install-production-to-corporate-server.sh"), "--help"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("DEPLOY_PATH=apps/work-on-holiday-production", result.stdout)
        self.assertIn("REMOTE_PORT=8082", result.stdout)
        self.assertIn(
            "REMOTE_DB_PATH=.local/share/work-on-holiday-production/survey_results.db",
            result.stdout,
        )
        self.assertIn(
            "REMOTE_ENV_FILE=.config/work-on-holiday-production/work-on-holiday-production.env",
            result.stdout,
        )
        self.assertIn("current instance on port 8081 is not changed", result.stdout)

    def test_production_update_help_documents_backup_and_isolation(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "update-production-corporate-server.sh"), "--help"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Production DB dump", result.stdout)
        self.assertIn("REMOTE_SERVICE_NAME=work-on-holiday-production", result.stdout)
        self.assertIn(
            "REMOTE_BACKUP_DIR=.local/state/work-on-holiday-production/backups",
            result.stdout,
        )
        self.assertIn("current instance on port 8081", result.stdout)

    def test_generic_install_and_update_forward_instance_paths(self) -> None:
        for script_name in (
            "install-to-corporate-server.sh",
            "update-corporate-server.sh",
        ):
            script_text = (SCRIPTS_DIR / script_name).read_text(encoding="utf-8")
            with self.subTest(script=script_name):
                self.assertIn(
                    '"INSTANCE_NAME=$(shell_quote "$REMOTE_INSTANCE_NAME")"',
                    script_text,
                )
                self.assertIn('"DB_PATH=$(shell_quote "$REMOTE_DB_PATH")"', script_text)
                self.assertIn(
                    '"BACKUP_DIR=$(shell_quote "$REMOTE_BACKUP_DIR")"',
                    script_text,
                )
                self.assertIn(
                    '"ENV_FILE=$(shell_quote "$REMOTE_ENV_FILE")"',
                    script_text,
                )
                self.assertIn(
                    '"STATE_DIR=$(shell_quote "$REMOTE_STATE_DIR")"',
                    script_text,
                )


if __name__ == "__main__":
    unittest.main()
