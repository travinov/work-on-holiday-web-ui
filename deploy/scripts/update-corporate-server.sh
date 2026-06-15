#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Update Work on Holiday from the local extracted ZIP folder to the corporate server.

Run this script on the local corporate workstation from the project root.
It creates a SQL dump of the existing remote SQLite DB before syncing the new
project version and applying DB schema updates.

Defaults:
  DEPLOY_HOST=tsles-assai0001.esrt.sber.ru
  DEPLOY_USER=CI09479675-lnx-travinov
  DEPLOY_PATH=apps/work-on-holiday
  REMOTE_DB_PATH=.local/share/work-on-holiday/survey_results.db
  REMOTE_BACKUP_DIR=.local/state/work-on-holiday/backups
  REMOTE_PORT=8081
  REMOTE_SERVICE_NAME=work-on-holiday

Command:
  deploy/scripts/update-corporate-server.sh
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

DEPLOY_HOST="${DEPLOY_HOST:-tsles-assai0001.esrt.sber.ru}"
DEPLOY_USER="${DEPLOY_USER:-CI09479675-lnx-travinov}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_PATH="${DEPLOY_PATH:-apps/work-on-holiday}"
REMOTE_DB_PATH="${REMOTE_DB_PATH:-.local/share/work-on-holiday/survey_results.db}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-.local/state/work-on-holiday/backups}"
REMOTE_HOST="${REMOTE_HOST:-127.0.0.1}"
REMOTE_PORT="${REMOTE_PORT:-8081}"
REMOTE_SERVICE_NAME="${REMOTE_SERVICE_NAME:-work-on-holiday}"
REMOTE_NO_USER_SYSTEMD="${REMOTE_NO_USER_SYSTEMD:-0}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:-}"

log() {
  printf '[update] %s\n' "$*"
}

fail() {
  printf '[update][error] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

shell_quote() {
  printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

require_cmd ssh
require_cmd rsync

if [[ ! -f "$PROJECT_ROOT/requirements.txt" || ! -f "$PROJECT_ROOT/src/web_ui.py" ]]; then
  fail "Run this script from the Work on Holiday project root"
fi

SSH_TARGET="$DEPLOY_USER@$DEPLOY_HOST"
SSH_BASE=(ssh -p "$DEPLOY_PORT")
if [[ -n "$SSH_OPTS" ]]; then
  # shellcheck disable=SC2206
  SSH_EXTRA=($SSH_OPTS)
  SSH_BASE+=("${SSH_EXTRA[@]}")
fi
RSYNC_SSH="ssh -p $DEPLOY_PORT $SSH_OPTS"

log "Target: $SSH_TARGET"
log "Remote project path: ~/$DEPLOY_PATH"
log "Remote SQLite DB file: ~/$REMOTE_DB_PATH"
log "Remote backup dir: ~/$REMOTE_BACKUP_DIR"

log "Creating SQL dump of current remote SQLite DB before update"
remote_dump_script=$(cat <<'REMOTE_DUMP'
set -euo pipefail
: "${REMOTE_DB_PATH:?REMOTE_DB_PATH is required}"
: "${REMOTE_BACKUP_DIR:?REMOTE_BACKUP_DIR is required}"

db_path="$HOME/$REMOTE_DB_PATH"
backup_dir="$HOME/$REMOTE_BACKUP_DIR"

if [ ! -f "$db_path" ]; then
  echo "Remote SQLite DB file not found: $db_path. Use install-to-corporate-server.sh for first install." >&2
  exit 10
fi

mkdir -p "$backup_dir"
timestamp="$(date +%Y%m%d-%H%M%S)"
dump_path="$backup_dir/survey_results-pre-update-$timestamp.sql"

python3 - "$db_path" "$dump_path" <<'PY'
from pathlib import Path
import sqlite3
import sys

db_path = Path(sys.argv[1])
dump_path = Path(sys.argv[2])
tmp_backup_path = dump_path.with_suffix(".snapshot.db")

source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
try:
    target = sqlite3.connect(tmp_backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
finally:
    source.close()

snapshot = sqlite3.connect(tmp_backup_path)
try:
    with dump_path.open("w", encoding="utf-8") as dump_file:
        dump_file.write("PRAGMA foreign_keys=OFF;\n")
        dump_file.write("BEGIN TRANSACTION;\n")
        for line in snapshot.iterdump():
            if line not in {"BEGIN TRANSACTION;", "COMMIT;"}:
                dump_file.write(f"{line}\n")
        dump_file.write("COMMIT;\n")
finally:
    snapshot.close()
    tmp_backup_path.unlink(missing_ok=True)

print(dump_path)
PY
REMOTE_DUMP
)
"${SSH_BASE[@]}" "$SSH_TARGET" \
  "REMOTE_DB_PATH=$(shell_quote "$REMOTE_DB_PATH") REMOTE_BACKUP_DIR=$(shell_quote "$REMOTE_BACKUP_DIR") bash -s" <<< "$remote_dump_script"

log "Creating remote project directory"
"${SSH_BASE[@]}" "$SSH_TARGET" "mkdir -p $(shell_quote "$DEPLOY_PATH")"

log "Syncing project files without local runtime data"
rsync -az --delete \
  -e "$RSYNC_SSH" \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'survey_results.db' \
  --exclude 'reports/' \
  --exclude 'generated_exports/' \
  --exclude 'restore_points/' \
  --exclude 'backups/' \
  $RSYNC_OPTS \
  "$PROJECT_ROOT/" "$SSH_TARGET:$DEPLOY_PATH/"

REMOTE_ENV=(
  "HOST=$(shell_quote "$REMOTE_HOST")"
  "PORT=$(shell_quote "$REMOTE_PORT")"
  "SERVICE_NAME=$(shell_quote "$REMOTE_SERVICE_NAME")"
  "NO_USER_SYSTEMD=$(shell_quote "$REMOTE_NO_USER_SYSTEMD")"
)

if [[ -n "${WORK_ON_HOLIDAY_SUPERUSER_LOGIN:-}" ]]; then
  REMOTE_ENV+=("WORK_ON_HOLIDAY_SUPERUSER_LOGIN=$(shell_quote "$WORK_ON_HOLIDAY_SUPERUSER_LOGIN")")
fi
if [[ -n "${WORK_ON_HOLIDAY_SUPERUSER_PASSWORD:-}" ]]; then
  REMOTE_ENV+=("WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=$(shell_quote "$WORK_ON_HOLIDAY_SUPERUSER_PASSWORD")")
fi
if [[ -n "${WORK_ON_HOLIDAY_SECURE_COOKIES:-}" ]]; then
  REMOTE_ENV+=("WORK_ON_HOLIDAY_SECURE_COOKIES=$(shell_quote "$WORK_ON_HOLIDAY_SECURE_COOKIES")")
fi

log "Running no-sudo user deploy on server and applying schema updates"
"${SSH_BASE[@]}" "$SSH_TARGET" "cd $(shell_quote "$DEPLOY_PATH") && ${REMOTE_ENV[*]} deploy/scripts/deploy-user-server.sh"

log "Update complete"
log "Server-local check: ssh $SSH_TARGET 'curl -I http://127.0.0.1:$REMOTE_PORT/'"
