#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy Work on Holiday on a Linux server.

Defaults assume the script is run from the project checkout on the server.

Environment overrides:
  APP_DIR                              Application directory. Default: project root.
  DB_PATH                              SQLite DB path. Default: $APP_DIR/survey_results.db.
  BACKUP_DIR                           DB backup directory. Default: $APP_DIR/backups.
  SERVICE_NAME                         systemd service name. Default: work-on-holiday.
  RUN_USER                             Linux user for systemd service. Default: workholiday.
  RUN_GROUP                            Linux group for systemd service. Default: $RUN_USER.
  HOST                                 Uvicorn bind host. Default: 127.0.0.1.
  PORT                                 Uvicorn bind port. Default: 8081.
  WORK_ON_HOLIDAY_SUPERUSER_LOGIN      Superuser login. Default: root.
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD   Required on first deploy unless env file already exists.
  WORK_ON_HOLIDAY_SECURE_COOKIES       Default: 1.
  WORK_ON_HOLIDAY_DB_PATH              App DB path. Usually set by this script from DB_PATH.
  SKIP_SYSTEMD                         Set to 1 to skip systemd install/restart.

Examples:
  sudo WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me' deploy/scripts/deploy-server.sh
  sudo APP_DIR=/opt/work-on-holiday DB_PATH=/var/lib/work-on-holiday/survey_results.db \
    WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me' deploy/scripts/deploy-server.sh
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

APP_DIR="${APP_DIR:-$PROJECT_ROOT}"
DB_PATH="${DB_PATH:-$APP_DIR/survey_results.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
SERVICE_NAME="${SERVICE_NAME:-work-on-holiday}"
RUN_USER="${RUN_USER:-workholiday}"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8081}"
ENV_DIR="${ENV_DIR:-/etc/work-on-holiday}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/work-on-holiday.env}"
SYSTEMD_UNIT="${SYSTEMD_UNIT:-/etc/systemd/system/$SERVICE_NAME.service}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SECURE_COOKIES="${WORK_ON_HOLIDAY_SECURE_COOKIES:-1}"
SUPERUSER_LOGIN="${WORK_ON_HOLIDAY_SUPERUSER_LOGIN:-root}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"

APP_DIR="$(cd "$APP_DIR" && pwd -P)"
DB_DIR="$(dirname "$DB_PATH")"
BACKUP_DIR="$(mkdir -p "$BACKUP_DIR" && cd "$BACKUP_DIR" && pwd -P)"

log() {
  printf '[deploy] %s\n' "$*"
}

fail() {
  printf '[deploy][error] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

ensure_env_key() {
  local key="$1"
  local value="$2"

  if ! grep -q "^${key}=" "$ENV_FILE"; then
    log "Adding $key to environment file"
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

backup_sqlite_db() {
  local source_db="$1"
  local backup_db="$2"
  "$APP_DIR/venv/bin/python" - "$source_db" "$backup_db" <<'PY'
import sqlite3
import sys

source_path, backup_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(source_path)
try:
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
finally:
    source.close()
PY
}

require_cmd "$PYTHON_BIN"

log "Application directory: $APP_DIR"
log "Database path: $DB_PATH"
log "Backup directory: $BACKUP_DIR"

cd "$APP_DIR"

if [[ ! -f "$APP_DIR/requirements.txt" || ! -f "$APP_DIR/src/web_ui.py" ]]; then
  fail "APP_DIR does not look like Work on Holiday project: $APP_DIR"
fi

if [[ ! -d "$APP_DIR/venv" ]]; then
  log "Creating virtual environment"
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi

log "Installing Python dependencies"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip >/dev/null
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$DB_DIR" "$BACKUP_DIR" "$APP_DIR/reports" "$APP_DIR/generated_exports"

if [[ -f "$DB_PATH" ]]; then
  timestamp="$(date +%Y%m%d-%H%M%S)"
  backup_path="$BACKUP_DIR/survey_results-$timestamp.db"
  log "Existing DB found. Creating backup: $backup_path"
  backup_sqlite_db "$DB_PATH" "$backup_path"
else
  log "DB does not exist. First deploy will create it."
fi

log "Initializing/upgrading DB schema without deleting data"
"$APP_DIR/venv/bin/python" "$APP_DIR/src/init_db.py" --db "$DB_PATH"

if [[ "$(id -u)" -eq 0 ]]; then
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    log "Creating system user: $RUN_USER"
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"
  fi
  chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR" "$DB_DIR" "$BACKUP_DIR"
fi

if [[ "$SKIP_SYSTEMD" == "1" ]]; then
  log "SKIP_SYSTEMD=1, systemd setup skipped"
  log "Deploy complete"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  fail "systemd setup requires root. Re-run with sudo or set SKIP_SYSTEMD=1."
fi

mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -z "${WORK_ON_HOLIDAY_SUPERUSER_PASSWORD:-}" ]]; then
    fail "WORK_ON_HOLIDAY_SUPERUSER_PASSWORD is required on first deploy"
  fi
  log "Creating environment file: $ENV_FILE"
  cat > "$ENV_FILE" <<ENV
WORK_ON_HOLIDAY_SUPERUSER_LOGIN=$SUPERUSER_LOGIN
WORK_ON_HOLIDAY_SUPERUSER_PASSWORD=$WORK_ON_HOLIDAY_SUPERUSER_PASSWORD
WORK_ON_HOLIDAY_SECURE_COOKIES=$SECURE_COOKIES
WORK_ON_HOLIDAY_DB_PATH=$DB_PATH
ENV
  chmod 600 "$ENV_FILE"
else
  log "Environment file exists, keeping it unchanged: $ENV_FILE"
  ensure_env_key "WORK_ON_HOLIDAY_DB_PATH" "$DB_PATH"
fi

log "Writing systemd unit: $SYSTEMD_UNIT"
cat > "$SYSTEMD_UNIT" <<UNIT
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
User=$RUN_USER
Group=$RUN_GROUP

[Install]
WantedBy=multi-user.target
UNIT

log "Reloading and restarting service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service" >/dev/null
systemctl restart "$SERVICE_NAME.service"
systemctl --no-pager --full status "$SERVICE_NAME.service" || true

log "Health check"
sleep 2
curl -fsS "http://$HOST:$PORT/" >/tmp/work-on-holiday-health.html
log "Deploy complete: http://$HOST:$PORT/"
