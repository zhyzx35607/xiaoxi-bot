#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="${1:-/opt/qqbot.backup-codex-fix-20260805-1535}"
APP_DIR="/opt/qqbot"
NAPCAT_CONFIG_DIR="/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config"
NAPCAT_ACCOUNT_CONFIG="napcat_3127014580.json"
test -d "$BACKUP_DIR"
test -f "$BACKUP_DIR/main.py"
test -f "$BACKUP_DIR/napcat-config/napcat.json"
test -f "$BACKUP_DIR/napcat-config/$NAPCAT_ACCOUNT_CONFIG"
restart_after_error() {
    systemctl start napcat.service || true
    systemctl start qqbot.service || true
}
trap restart_after_error ERR
systemctl stop qqbot.service
rsync -a --delete --exclude 'venv/' --exclude 'data/' "$BACKUP_DIR/" "$APP_DIR/"
install -m 600 "$BACKUP_DIR/napcat-config/napcat.json" "$NAPCAT_CONFIG_DIR/napcat.json"
install -m 600 "$BACKUP_DIR/napcat-config/$NAPCAT_ACCOUNT_CONFIG" "$NAPCAT_CONFIG_DIR/$NAPCAT_ACCOUNT_CONFIG"
systemctl restart napcat.service
systemctl start qqbot.service
systemctl is-active --quiet napcat.service
systemctl is-active --quiet qqbot.service
trap - ERR
printf 'ROLLBACK_OK backup=%s\n' "$BACKUP_DIR"
