#!/usr/bin/env python3
"""Save and verify an existing instance before its files are overwritten.

Uses only the Python standard library so the new application need not be
installed on the server yet. This command never restores or stops an instance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone


def resolve_path(value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path.home() / path).resolve()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_database(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed: " + str(result))
    finally:
        conn.close()


def verify_restore_point(folder):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    required = {"application.tar.gz", "application.env", "database.sqlite", "database.sql",
                "start.sh", "watchdog.sh", "crontab-reference.txt", "README.txt"}
    if manifest.get("format_version") != 1 or not required.issubset(manifest["files"]):
        raise RuntimeError("Incomplete restore point manifest")
    for name, expected in manifest["files"].items():
        path = folder / name
        if path.parent != folder or path.is_symlink() or not path.is_file():
            raise RuntimeError("Invalid restore point file: " + name)
        if path.stat().st_size != expected["size"] or sha256(path) != expected["sha256"]:
            raise RuntimeError("Restore point checksum mismatch: " + name)
    check_database(folder / "database.sqlite")
    # Read every member, not just the tar header, to check archive decompression.
    with tarfile.open(folder / "application.tar.gz", "r:gz") as archive:
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as stream:
                    while stream.read(1024 * 1024):
                        pass
    # Prove the SQL dump can also be restored without touching the live database.
    with tempfile.TemporaryDirectory(prefix="woh-restore-check-") as temporary:
        restored = Path(temporary) / "restored.sqlite"
        conn = sqlite3.connect(restored)
        try:
            conn.executescript((folder / "database.sql").read_text(encoding="utf-8"))
        finally:
            conn.close()
        check_database(restored)
    return manifest


def create_restore_point(app_dir, db_path, env_file, backup_dir, instance, service, state_dir, host, port):
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (instance, service)):
        raise ValueError("Instance and service names must not contain path separators")
    if not (app_dir / "src/web_ui.py").is_file() or not (app_dir / "requirements.txt").is_file():
        raise RuntimeError("Previous application not found: " + str(app_dir))
    if not db_path.is_file() or not env_file.is_file():
        raise RuntimeError("Existing database and environment file are required for an update")
    if not (app_dir / "venv/bin/python").exists():
        raise RuntimeError("Previous Python environment not found: " + str(app_dir / "venv"))
    # The configured backup directory must not contain the application itself.
    if backup_dir == app_dir or backup_dir in app_dir.parents:
        raise RuntimeError("Backup directory must not contain the application directory")
    points = backup_dir / "restore-points"
    points.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    pending = Path(tempfile.mkdtemp(prefix=".incomplete-" + stamp + "-", dir=points))
    final = points / ("pre-update-" + pending.name[len(".incomplete-"):])
    try:
        snapshot = pending / "database.sqlite"
        source = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            with (pending / "database.sql").open("w", encoding="utf-8") as stream:
                for line in target.iterdump():
                    stream.write(line + "\n")
        finally:
            target.close()
            source.close()
        check_database(snapshot)

        exclusions = {".git", ".playwright-cli", "reports", "generated_exports", "restore_points", "backups", "output"}
        def archive_filter(member):
            relative = Path(member.name).relative_to("application")
            parts = relative.parts
            if parts and parts[0] in exclusions:
                return None
            if "__pycache__" in parts or relative.name == ".DS_Store":
                return None
            source_path = app_dir / relative
            if source_path == db_path or source_path in {Path(str(db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")}:
                return None
            if source_path == backup_dir or backup_dir in source_path.parents:
                return None
            return member

        with tarfile.open(pending / "application.tar.gz", "w:gz", dereference=False) as archive:
            archive.add(app_dir, arcname="application", filter=archive_filter)
        shutil.copy2(env_file, pending / "application.env")
        runtime = {}
        for suffix in ("start", "watchdog"):
            source_path = Path.home() / ".local/bin" / (service + "-" + suffix + ".sh")
            if not source_path.is_file():
                raise RuntimeError("Existing startup script not found: " + str(source_path))
            name = suffix + ".sh"
            shutil.copy2(source_path, pending / name)
            runtime[name] = str(source_path)
        # Keep cron as a reference only: restoring a whole crontab could affect
        # other applications or the separate Production instance.
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if cron.returncode != 0 and not (cron.returncode == 1 and "no crontab" in cron.stderr.lower()):
            raise RuntimeError("Cannot save existing crontab: " + cron.stderr.strip())
        (pending / "crontab-reference.txt").write_text(cron.stdout, encoding="utf-8")
        python_version = subprocess.run([str(app_dir / "venv/bin/python"), "--version"],
                                        check=True, capture_output=True, text=True)
        (pending / "README.txt").write_text(
            "Комплект для ручного отката Work on Holiday\n\n"
            "Автоматическое восстановление не выполняется.\n"
            "Пути экземпляра, порт и контрольные суммы указаны в manifest.json.\n"
            "application.tar.gz содержит предыдущий код и venv под каталогом application/.\n"
            "database.sqlite — согласованная копия SQLite; database.sql — её SQL-dump.\n"
            "application.env содержит настройки, включая секреты; не публикуйте этот комплект.\n"
            "start.sh и watchdog.sh — прежние скрипты запуска. После копирования верните им право исполнения.\n"
            "crontab-reference.txt — справочная копия, не заменяйте ею весь текущий crontab.\n\n"
            "Порядок: проверить комплект; сохранить текущее состояние отдельно; временно отключить\n"
            "watchdog именно этого экземпляра; остановить приложение и проверить остановку;\n"
            "восстановить код и venv по прежнему абсолютному пути, env и скрипты запуска;\n"
            "при полном откате заменить БД, предварительно сохранив её вместе с файлами -wal/-shm;\n"
            "запустить прежнюю версию, проверить вход и заявки, затем вернуть её watchdog.\n"
            "Не запускайте скрипт обновления после восстановления: он переустанавливает зависимости.\n"
            "Не переносите этот venv на другой сервер или путь. Системный Python должен остаться прежним.\n"
            "При восстановлении database.sqlite изменения после снимка в неё не попадут.\n"
            "Подробная инструкция находится в новой сборке: deploy/MANUAL_ROLLBACK.md.\n",
            encoding="utf-8",
        )
        manifest = dict(
            format_version=1, created_at_utc=stamp, instance=instance, service=service,
            app_dir=str(app_dir), db_path=str(db_path), env_file=str(env_file),
            backup_dir=str(backup_dir), state_dir=str(state_dir), host=host, port=port,
            python_version=python_version.stdout.strip() or python_version.stderr.strip(),
            runtime_files=runtime,
            excluded_application_dirs=sorted(exclusions),
            files={path.name: dict(size=path.stat().st_size, sha256=sha256(path))
                   for path in pending.iterdir() if path.is_file()},
        )
        (pending / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Includes passwords and database content; owner-only files, even if the
        # original environment file had broader permissions.
        for path in pending.iterdir():
            path.chmod(0o600)
        verify_restore_point(pending)
        pending.rename(final)
    except Exception:
        shutil.rmtree(pending)
        raise
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="Verify an existing restore point without restoring it")
    parser.add_argument("--app-dir")
    parser.add_argument("--db-path")
    parser.add_argument("--env-file")
    parser.add_argument("--backup-dir")
    parser.add_argument("--state-dir")
    parser.add_argument("--instance")
    parser.add_argument("--service")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.verify:
        verify_restore_point(args.verify.resolve())
        print("Restore point verified: " + str(args.verify))
        return
    required = ("app_dir", "db_path", "env_file", "backup_dir", "state_dir", "instance", "service", "host", "port")
    if any(getattr(args, name) is None for name in required):
        parser.error("Creation requires all instance paths and settings")
    result = create_restore_point(
        resolve_path(args.app_dir), resolve_path(args.db_path), resolve_path(args.env_file),
        resolve_path(args.backup_dir), args.instance, args.service, resolve_path(args.state_dir), args.host, args.port,
    )
    print("Restore point verified: " + str(result))


if __name__ == "__main__":
    main()
