#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy Work on Holiday without sudo from the current ZIP/extracted project folder.

This mode does not write to /opt, /var/lib, /var/backups, /etc, or system systemd.
It uses user-writable paths, a detached screen session, and a crontab watchdog.

Defaults:
  APP_DIR=current project root
  DB_PATH=$HOME/.local/share/work-on-holiday/survey_results.db
  BACKUP_DIR=$HOME/.local/state/work-on-holiday/backups
  ENV_FILE=$HOME/.config/work-on-holiday/work-on-holiday.env
  SCREEN_NAME=work-on-holiday
  PORT=8081

Required on first deploy:
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD

The watchdog installs two crontab entries:
  @reboot start script
  * * * * * health-check and restart script
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
SERVICE_NAME="${SERVICE_NAME:-work-on-holiday}"
SCREEN_NAME="${SCREEN_NAME:-$SERVICE_NAME}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8081}"
SUPERUSER_LOGIN="${WORK_ON_HOLIDAY_SUPERUSER_LOGIN:-root}"
SECURE_COOKIES="${WORK_ON_HOLIDAY_SECURE_COOKIES:-0}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/work-on-holiday}"
LOG_DIR="${LOG_DIR:-$STATE_DIR/logs}"
START_SCRIPT="${START_SCRIPT:-$BIN_DIR/$SERVICE_NAME-start.sh}"
WATCHDOG_SCRIPT="${WATCHDOG_SCRIPT:-$BIN_DIR/$SERVICE_NAME-watchdog.sh}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/$SERVICE_NAME.log}"

log() {
  printf '[user-deploy] %s\n' "$*"
}

fail() {
  printf '[user-deploy][error] %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

ensure_env_key() {
  local key="$1"
  local value="$2"

  if ! grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

APP_DIR="$(cd "$APP_DIR" && pwd -P)"
mkdir -p "$(dirname "$DB_PATH")" "$BACKUP_DIR" "$ENV_DIR" "$BIN_DIR" "$LOG_DIR"

require_cmd screen
require_cmd crontab
require_cmd curl

if command -v systemctl >/dev/null 2>&1; then
  log "Stopping old user systemd service if it exists"
  systemctl --user stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true
  systemctl --user disable "$SERVICE_NAME.service" >/dev/null 2>&1 || true
fi

log "Stopping existing screen session if it exists"
screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
sleep 1

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

log "Writing screen start script: $START_SCRIPT"
cat > "$START_SCRIPT" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail

APP_DIR='$APP_DIR'
ENV_FILE='$ENV_FILE'
LOG_FILE='$LOG_FILE'
SCREEN_NAME='$SCREEN_NAME'
HOST='$HOST'
PORT='$PORT'

mkdir -p "\$(dirname "\$LOG_FILE")"

if screen -ls | grep -q "[.]\$SCREEN_NAME[[:space:]]"; then
  exit 0
fi

screen -dmS "\$SCREEN_NAME" bash -c '
set -euo pipefail
app_dir="\$1"
env_file="\$2"
log_file="\$3"
host="\$4"
port="\$5"
cd "\$app_dir"
set -a
. "\$env_file"
set +a
exec "\$app_dir/venv/bin/python" -m uvicorn src.web_ui:app --host "\$host" --port "\$port" --proxy-headers --forwarded-allow-ips=127.0.0.1 >> "\$log_file" 2>&1
' _ "\$APP_DIR" "\$ENV_FILE" "\$LOG_FILE" "\$HOST" "\$PORT"
SCRIPT
chmod +x "$START_SCRIPT"

log "Writing watchdog script: $WATCHDOG_SCRIPT"
cat > "$WATCHDOG_SCRIPT" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail

START_SCRIPT='$START_SCRIPT'
SCREEN_NAME='$SCREEN_NAME'
PORT='$PORT'

if curl -fsS --max-time 5 "http://127.0.0.1:\$PORT/" >/dev/null 2>&1; then
  exit 0
fi

screen -S "\$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
"\$START_SCRIPT"
SCRIPT
chmod +x "$WATCHDOG_SCRIPT"

log "Starting screen session: $SCREEN_NAME"
"$START_SCRIPT"

log "Installing crontab watchdog"
tmp_cron="$(mktemp)"
tmp_new_cron="$(mktemp)"
crontab -l > "$tmp_cron" 2>/dev/null || true
grep -Fv "$START_SCRIPT" "$tmp_cron" | grep -Fv "$WATCHDOG_SCRIPT" > "$tmp_new_cron" || true
{
  cat "$tmp_new_cron"
  printf '@reboot %s >/dev/null 2>&1\n' "$START_SCRIPT"
  printf '* * * * * %s >/dev/null 2>&1\n' "$WATCHDOG_SCRIPT"
} | crontab -
rm -f "$tmp_cron" "$tmp_new_cron"

log "Health check"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    log "Deploy complete: http://$HOST:$PORT/"
    log "Runtime: screen session '$SCREEN_NAME' plus crontab watchdog"
    exit 0
  fi
  sleep 1
done

tail -n 80 "$LOG_FILE" >&2 || true
fail "Application did not pass health check on http://127.0.0.1:$PORT/"
