#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy Work on Holiday without sudo from the current ZIP/extracted project folder.

This mode does not write to /opt, /var/lib, /var/backups, /etc, or system systemd.
It uses user-writable paths and a user systemd service.

Defaults:
  APP_DIR=current project root
  DB_PATH=$HOME/.local/share/work-on-holiday/survey_results.db
  BACKUP_DIR=$HOME/.local/state/work-on-holiday/backups
  ENV_FILE=$HOME/.config/work-on-holiday/work-on-holiday.env
  PORT=8081

Required on first deploy:
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD

Examples:
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-first-deploy' \
    deploy/scripts/deploy-user-server.sh

  deploy/scripts/deploy-user-server.sh

Manual run without user systemd:
  NO_USER_SYSTEMD=1 WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me' \
    deploy/scripts/deploy-user-server.sh
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

APP_DIR="${APP_DIR:-$PROJECT_ROOT}"
DB_PATH="${DB_PATH:-$HOME/.local/share/work-on-holiday/survey_results.db}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/state/work-on-holiday/backups}"
ENV_DIR="${ENV_DIR:-$HOME/.config/work-on-holiday}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/work-on-holiday.env}"
USER_SYSTEMD_DIR="${USER_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
SERVICE_NAME="${SERVICE_NAME:-work-on-holiday}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8081}"
SUPERUSER_LOGIN="${WORK_ON_HOLIDAY_SUPERUSER_LOGIN:-root}"
SECURE_COOKIES="${WORK_ON_HOLIDAY_SECURE_COOKIES:-0}"
NO_USER_SYSTEMD="${NO_USER_SYSTEMD:-0}"

log() {
  printf '[user-deploy] %s\n' "$*"
}

fail() {
  printf '[user-deploy][error] %s\n' "$*" >&2
  exit 1
}

ensure_env_key() {
  local key="$1"
  local value="$2"

  if ! grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

APP_DIR="$(cd "$APP_DIR" && pwd -P)"
mkdir -p "$(dirname "$DB_PATH")" "$BACKUP_DIR" "$ENV_DIR"

log "Preparing app without sudo"
APP_DIR="$APP_DIR" \
DB_PATH="$DB_PATH" \
BACKUP_DIR="$BACKUP_DIR" \
SKIP_SYSTEMD=1 \
PORT="$PORT" \
WORK_ON_HOLIDAY_SUPERUSER_PASSWORD="${WORK_ON_HOLIDAY_SUPERUSER_PASSWORD:-}" \
"$SCRIPT_DIR/deploy-server.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -z "${WORK_ON_HOLIDAY_SUPERUSER_PASSWORD:-}" ]]; then
    fail "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD is required on first deploy"
  fi

  log "Creating user environment file: $ENV_FILE"
  cat > "$ENV_FILE" <<ENV
WORK_ON_HOLIDAY_SUPERUSER_LOGIN=$SUPERUSER_LOGIN
WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=$WORK_ON_HOLIDAY_SUPERUSER_PASSWORD
WORK_ON_HOLIDAY_SECURE_COOKIES=$SECURE_COOKIES
WORK_ON_HOLIDAY_DB_PATH=$DB_PATH
ENV
  chmod 600 "$ENV_FILE"
else
  log "User environment file exists, keeping it unchanged: $ENV_FILE"
  ensure_env_key "WORK_ON_HOLIDAY_DB_PATH" "$DB_PATH"
fi

if [[ "$NO_USER_SYSTEMD" == "1" ]]; then
  log "NO_USER_SYSTEMD=1, user systemd setup skipped"
  log "Manual start command:"
  printf '  set -a && . %q && set +a && %q/venv/bin/python -m uvicorn src.web_ui:app --host %q --port %q\n' "$ENV_FILE" "$APP_DIR" "$HOST" "$PORT"
  exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
  fail "systemctl not found. Re-run with NO_USER_SYSTEMD=1 for manual start."
fi

mkdir -p "$USER_SYSTEMD_DIR"
unit_path="$USER_SYSTEMD_DIR/$SERVICE_NAME.service"

log "Writing user systemd unit: $unit_path"
cat > "$unit_path" <<UNIT
[Unit]
Description=Work on Holiday FastAPI app
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python -m uvicorn src.web_ui:app --host $HOST --port $PORT --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT

log "Reloading and restarting user service"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME.service" >/dev/null
systemctl --user restart "$SERVICE_NAME.service"
systemctl --user --no-pager --full status "$SERVICE_NAME.service" || true

log "Deploy complete: http://$HOST:$PORT/"
log "If the service stops after logout, ask server admins to enable lingering for this user."
