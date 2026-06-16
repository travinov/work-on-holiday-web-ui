#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Initial install of Work on Holiday from the local extracted ZIP folder to the corporate server.

Run this script on the local corporate workstation from the project root.
It refuses to continue if the remote SQLite DB or env file already exists.
Use update-corporate-server.sh for repeat deployments.

Defaults:
  DEPLOY_HOST=tsles-assai0001.esrt.sber.ru
  DEPLOY_USER=CI09479675-lnx-travinov
  DEPLOY_PATH=apps/work-on-holiday
  REMOTE_HOST=0.0.0.0
  REMOTE_PORT=8081
  REMOTE_SERVICE_NAME=work-on-holiday
  WORK_ON_HOLIDAY_SUPERUSER_LOGIN=root

Command:
  deploy/scripts/install-to-corporate-server.sh

The script prompts for the superuser secret if it is not already provided in
the environment.
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
REMOTE_HOST="${REMOTE_HOST:-0.0.0.0}"
REMOTE_PORT="${REMOTE_PORT:-8081}"
REMOTE_SERVICE_NAME="${REMOTE_SERVICE_NAME:-work-on-holiday}"
REMOTE_DB_PATH="${REMOTE_DB_PATH:-.local/share/work-on-holiday/survey_results.db}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-.config/work-on-holiday/work-on-holiday.env}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:-}"

log() {
  printf '[install] %s\n' "$*"
}

fail() {
  printf '[install][error] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

shell_quote() {
  printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

SUPERUSER_SECRET="${WORK_ON_HOLIDAY_SUPERUSER_PASSWORD:-}"
if [[ -z "$SUPERUSER_SECRET" ]]; then
  if [[ -t 0 ]]; then
    printf 'Superuser login: %s\n' "${WORK_ON_HOLIDAY_SUPERUSER_LOGIN:-root}"
    printf 'Enter superuser secret: '
    IFS= read -r -s SUPERUSER_SECRET
    printf '\n'
  else
    fail "Superuser secret is required for initial install"
  fi
fi
[[ -n "$SUPERUSER_SECRET" ]] || fail "Superuser secret cannot be empty"

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

log "Checking that this is a first install"
"${SSH_BASE[@]}" "$SSH_TARGET" \
  "db=\$HOME/$(shell_quote "$REMOTE_DB_PATH"); env=\$HOME/$(shell_quote "$REMOTE_ENV_FILE"); if [ -e \"\$db\" ] || [ -e \"\$env\" ]; then echo 'Remote DB or env file already exists. Use update-corporate-server.sh.' >&2; exit 10; fi"

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
  "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=$(shell_quote "$SUPERUSER_SECRET")"
)

if [[ -n "${WORK_ON_HOLIDAY_SUPERUSER_LOGIN:-}" ]]; then
  REMOTE_ENV+=("WORK_ON_HOLIDAY_SUPERUSER_LOGIN=$(shell_quote "$WORK_ON_HOLIDAY_SUPERUSER_LOGIN")")
fi
if [[ -n "${WORK_ON_HOLIDAY_SECURE_COOKIES:-}" ]]; then
  REMOTE_ENV+=("WORK_ON_HOLIDAY_SECURE_COOKIES=$(shell_quote "$WORK_ON_HOLIDAY_SECURE_COOKIES")")
fi

log "Running first no-sudo user deploy on server"
"${SSH_BASE[@]}" "$SSH_TARGET" "cd $(shell_quote "$DEPLOY_PATH") && ${REMOTE_ENV[*]} deploy/scripts/deploy-user-server.sh"

log "Initial install complete"
log "Server-local check: ssh $SSH_TARGET 'curl -I http://127.0.0.1:$REMOTE_PORT/'"
