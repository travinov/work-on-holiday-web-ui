#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Update the isolated Work on Holiday Production instance.

Run this script on the local corporate workstation from the extracted project
folder. It creates a Production DB dump before updating application files and
does not change the current instance on port 8081.

Production defaults:
  DEPLOY_PATH=apps/work-on-holiday-production
  REMOTE_INSTANCE_NAME=work-on-holiday-production
  REMOTE_PORT=8082
  REMOTE_SERVICE_NAME=work-on-holiday-production
  REMOTE_DB_PATH=.local/share/work-on-holiday-production/survey_results.db
  REMOTE_BACKUP_DIR=.local/state/work-on-holiday-production/backups
  REMOTE_ENV_FILE=.config/work-on-holiday-production/work-on-holiday-production.env

Command:
  deploy/scripts/update-production-corporate-server.sh
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

export DEPLOY_PATH="${DEPLOY_PATH:-apps/work-on-holiday-production}"
export REMOTE_INSTANCE_NAME="${REMOTE_INSTANCE_NAME:-work-on-holiday-production}"
export REMOTE_HOST="${REMOTE_HOST:-0.0.0.0}"
export REMOTE_PORT="${REMOTE_PORT:-8082}"
export REMOTE_SERVICE_NAME="${REMOTE_SERVICE_NAME:-work-on-holiday-production}"
export REMOTE_DB_PATH="${REMOTE_DB_PATH:-.local/share/work-on-holiday-production/survey_results.db}"
export REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-.local/state/work-on-holiday-production/backups}"
export REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-.config/work-on-holiday-production/work-on-holiday-production.env}"
export REMOTE_STATE_DIR="${REMOTE_STATE_DIR:-.local/state/work-on-holiday-production}"

exec "$SCRIPT_DIR/update-corporate-server.sh" "$@"
