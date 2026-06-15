#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy Work on Holiday from the current ZIP/extracted project folder on the corporate server.

Run this script from the extracted project folder on the server.

Production defaults:
  APP_DIR=/current/project/root
  DB_PATH=/var/lib/work-on-holiday/survey_results.db
  BACKUP_DIR=/var/backups/work-on-holiday
  PORT=8081

Required on first deploy:
  WORK_ON_HOLIDAY_SUPERUSER_PASSWORD

Example:
  sudo WORK_ON_HOLIDAY_SUPERUSER_PASSWORD='change-me-on-first-deploy' \
    deploy/scripts/deploy-corporate-server.sh

Repeat deploy:
  sudo deploy/scripts/deploy-corporate-server.sh
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

export APP_DIR="${APP_DIR:-$PROJECT_ROOT}"
export DB_PATH="${DB_PATH:-/var/lib/work-on-holiday/survey_results.db}"
export BACKUP_DIR="${BACKUP_DIR:-/var/backups/work-on-holiday}"
export PORT="${PORT:-8081}"
export SERVICE_NAME="${SERVICE_NAME:-work-on-holiday}"

exec "$SCRIPT_DIR/deploy-server.sh"
