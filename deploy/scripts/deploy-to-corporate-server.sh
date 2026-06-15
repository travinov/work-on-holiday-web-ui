#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy Work on Holiday from the local extracted ZIP folder to the corporate server.

Run this script on the local corporate workstation from the project root.
It copies the current local folder to the server over SSH/rsync and runs no-sudo
user deployment on the server.

Defaults:
  DEPLOY_HOST=tsles-assai0001.esrt.sber.ru
  DEPLOY_USER=CI09479675-lnx-travinov
  DEPLOY_PATH=apps/work-on-holiday
  REMOTE_PORT=8081
  REMOTE_SERVICE_NAME=work-on-holiday

Required on first deploy:
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD

First deploy:
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-first-deploy' \
    deploy/scripts/deploy-to-corporate-server.sh

Repeat deploy:
  deploy/scripts/deploy-to-corporate-server.sh
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
REMOTE_HOST="${REMOTE_HOST:-127.0.0.1}"
REMOTE_PORT="${REMOTE_PORT:-8081}"
REMOTE_SERVICE_NAME="${REMOTE_SERVICE_NAME:-work-on-holiday}"
REMOTE_NO_USER_SYSTEMD="${REMOTE_NO_USER_SYSTEMD:-0}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:-}"

log() {
  printf '[corp-deploy] %s\n' "$*"
}

fail() {
  printf '[corp-deploy][error] %s\n' "$*" >&2
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
log "Remote app port: $REMOTE_PORT"

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

log "Running no-sudo user deploy on server"
"${SSH_BASE[@]}" "$SSH_TARGET" "cd $(shell_quote "$DEPLOY_PATH") && ${REMOTE_ENV[*]} deploy/scripts/deploy-user-server.sh"

log "Deploy complete"
log "Server-local check: ssh $SSH_TARGET 'curl -I http://127.0.0.1:$REMOTE_PORT/'"
