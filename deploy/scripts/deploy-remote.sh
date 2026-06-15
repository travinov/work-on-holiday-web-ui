#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy Work on Holiday from a local workstation to a corporate Linux server.

Required:
  DEPLOY_HOST                           SSH host, for example tsles-assai.example.corp.
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD   Required only on first server deploy unless env file exists.

Optional:
  DEPLOY_USER                           SSH user. Default: current local user.
  DEPLOY_PORT                           SSH port. Default: 22.
  DEPLOY_PATH                           App path on server. Default: /opt/work-on-holiday.
  REMOTE_DB_PATH                        Server DB path. Default: $DEPLOY_PATH/survey_results.db.
  REMOTE_BACKUP_DIR                     Server backup dir. Default: $DEPLOY_PATH/backups.
  REMOTE_SERVICE_NAME                   systemd service name. Default: work-on-holiday.
  REMOTE_RUN_USER                       Linux service user. Default: workholiday.
  REMOTE_HOST                           Uvicorn bind host on server. Default: 127.0.0.1.
  REMOTE_PORT                           Uvicorn bind port on server. Default: 8081.
  SSH_OPTS                              Extra ssh options.
  RSYNC_OPTS                            Extra rsync options.
  REMOTE_SKIP_SYSTEMD                   Set to 1 to skip systemd setup on server.

Examples:
  DEPLOY_HOST=tsles-assai0001.esrt.sber.ru \
    WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me' \
    deploy/scripts/deploy-remote.sh

  DEPLOY_USER=CI09479675-lnx-travinov DEPLOY_HOST=tsles-assai0001.esrt.sber.ru DEPLOY_PATH=/opt/work-on-holiday \
    WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me' \
    deploy/scripts/deploy-remote.sh
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

DEPLOY_HOST="${DEPLOY_HOST:-}"
DEPLOY_USER="${DEPLOY_USER:-$(whoami)}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/work-on-holiday}"
REMOTE_DB_PATH="${REMOTE_DB_PATH:-$DEPLOY_PATH/survey_results.db}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-$DEPLOY_PATH/backups}"
REMOTE_SERVICE_NAME="${REMOTE_SERVICE_NAME:-work-on-holiday}"
REMOTE_RUN_USER="${REMOTE_RUN_USER:-workholiday}"
REMOTE_HOST="${REMOTE_HOST:-127.0.0.1}"
REMOTE_PORT="${REMOTE_PORT:-8081}"
REMOTE_SKIP_SYSTEMD="${REMOTE_SKIP_SYSTEMD:-0}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:-}"

log() {
  printf '[remote-deploy] %s\n' "$*"
}

fail() {
  printf '[remote-deploy][error] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

shell_quote() {
  printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

[[ -n "$DEPLOY_HOST" ]] || fail "DEPLOY_HOST is required. Example: DEPLOY_HOST=tsles-assai0001.esrt.sber.ru"
require_cmd ssh
require_cmd rsync

SSH_TARGET="$DEPLOY_USER@$DEPLOY_HOST"
SSH_BASE=(ssh -p "$DEPLOY_PORT")
if [[ -n "$SSH_OPTS" ]]; then
  # shellcheck disable=SC2206
  SSH_EXTRA=($SSH_OPTS)
  SSH_BASE+=("${SSH_EXTRA[@]}")
fi

RSYNC_SSH="ssh -p $DEPLOY_PORT $SSH_OPTS"

log "Target: $SSH_TARGET"
log "Remote app path: $DEPLOY_PATH"
log "Remote DB path: $REMOTE_DB_PATH"
log "Remote backup dir: $REMOTE_BACKUP_DIR"

log "Creating remote directory"
"${SSH_BASE[@]}" "$SSH_TARGET" "sudo mkdir -p '$DEPLOY_PATH' && sudo chown -R '$DEPLOY_USER':'$DEPLOY_USER' '$DEPLOY_PATH'"

log "Syncing project files without local data"
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

log "Running server deploy script"
REMOTE_ENV=(
  "APP_DIR=$(shell_quote "$DEPLOY_PATH")"
  "DB_PATH=$(shell_quote "$REMOTE_DB_PATH")"
  "BACKUP_DIR=$(shell_quote "$REMOTE_BACKUP_DIR")"
  "SERVICE_NAME=$(shell_quote "$REMOTE_SERVICE_NAME")"
  "RUN_USER=$(shell_quote "$REMOTE_RUN_USER")"
  "HOST=$(shell_quote "$REMOTE_HOST")"
  "PORT=$(shell_quote "$REMOTE_PORT")"
  "SKIP_SYSTEMD=$(shell_quote "$REMOTE_SKIP_SYSTEMD")"
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

"${SSH_BASE[@]}" "$SSH_TARGET" "cd '$DEPLOY_PATH' && sudo ${REMOTE_ENV[*]} deploy/scripts/deploy-server.sh"

log "Remote deploy complete"
