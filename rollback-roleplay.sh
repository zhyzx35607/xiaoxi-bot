#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="${1:?usage: rollback-roleplay.sh /opt/qqbot.backup-roleplay-TIMESTAMP}"
APP_DIR="/opt/qqbot"
test -d "$BACKUP_DIR"
test -f "$BACKUP_DIR/main.py"
restart_after_error() {
    systemctl start qqbot.service || true
}
trap restart_after_error ERR
systemctl stop qqbot.service
rsync -a --delete --exclude 'venv/' --exclude 'data/' "$BACKUP_DIR/" "$APP_DIR/"
systemctl start qqbot.service
systemctl is-active --quiet qqbot.service
trap - ERR
printf 'ROLLBACK_OK backup=%s\n' "$BACKUP_DIR"
