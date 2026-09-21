#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Update Work on Holiday from the local extracted ZIP folder to the corporate server.

Run this script on the local corporate workstation from the project root.
Before syncing files it creates and verifies a restore point: previous code and
venv, SQLite snapshot and SQL dump, environment file, startup scripts and cron
reference. Backup failure stops the update. Restoring remains a manual action.

Defaults:
  DEPLOY_HOST=tsles-assai0001.esrt.sber.ru
  DEPLOY_USER=CI09479675-lnx-travinov
  DEPLOY_PATH=apps/work-on-holiday
  REMOTE_INSTANCE_NAME=work-on-holiday
  REMOTE_DB_PATH=.local/share/work-on-holiday/survey_results.db
  REMOTE_BACKUP_DIR=.local/state/work-on-holiday/backups
  REMOTE_HOST=0.0.0.0
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
REMOTE_INSTANCE_NAME="${REMOTE_INSTANCE_NAME:-work-on-holiday}"
REMOTE_DB_PATH="${REMOTE_DB_PATH:-.local/share/$REMOTE_INSTANCE_NAME/survey_results.db}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-.local/state/$REMOTE_INSTANCE_NAME/backups}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-.config/$REMOTE_INSTANCE_NAME/$REMOTE_INSTANCE_NAME.env}"
REMOTE_STATE_DIR="${REMOTE_STATE_DIR:-.local/state/$REMOTE_INSTANCE_NAME}"
REMOTE_HOST="${REMOTE_HOST:-0.0.0.0}"
REMOTE_PORT="${REMOTE_PORT:-8081}"
REMOTE_SERVICE_NAME="${REMOTE_SERVICE_NAME:-$REMOTE_INSTANCE_NAME}"
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

log "Saving and verifying the previous version before update"
RESTORE_ARGS=(
  --app-dir "$DEPLOY_PATH"
  --db-path "$REMOTE_DB_PATH"
  --backup-dir "$REMOTE_BACKUP_DIR"
  --env-file "$REMOTE_ENV_FILE"
  --state-dir "$REMOTE_STATE_DIR"
  --instance "$REMOTE_INSTANCE_NAME"
  --service "$REMOTE_SERVICE_NAME"
  --host "$REMOTE_HOST"
  --port "$REMOTE_PORT"
)
RESTORE_COMMAND="python3 -"
for argument in "${RESTORE_ARGS[@]}"; do
  RESTORE_COMMAND+=" $(shell_quote "$argument")"
done
# Stream the new helper before rsync: even the first update from an older
# installation saves the OLD files, without requiring the helper on the server.
"${SSH_BASE[@]}" "$SSH_TARGET" "$RESTORE_COMMAND" < "$SCRIPT_DIR/create-restore-point.py"

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
  --exclude 'data/' \
  $RSYNC_OPTS \
  "$PROJECT_ROOT/" "$SSH_TARGET:$DEPLOY_PATH/"

log "Removing legacy bundled employee data from the remote project"
"${SSH_BASE[@]}" "$SSH_TARGET" "rm -rf $(shell_quote "$DEPLOY_PATH/data")"

REMOTE_ENV=(
  "INSTANCE_NAME=$(shell_quote "$REMOTE_INSTANCE_NAME")"
  "DB_PATH=$(shell_quote "$REMOTE_DB_PATH")"
  "BACKUP_DIR=$(shell_quote "$REMOTE_BACKUP_DIR")"
  "ENV_FILE=$(shell_quote "$REMOTE_ENV_FILE")"
  "STATE_DIR=$(shell_quote "$REMOTE_STATE_DIR")"
  "HOST=$(shell_quote "$REMOTE_HOST")"
  "PORT=$(shell_quote "$REMOTE_PORT")"
  "SERVICE_NAME=$(shell_quote "$REMOTE_SERVICE_NAME")"
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
log "Server-local check: ssh $SSH_TARGET \"curl -sS -o /dev/null -w 'HTTP:%{http_code}\\n' http://127.0.0.1:$REMOTE_PORT/\""
