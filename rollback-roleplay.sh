#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="${1:-/opt/qqbot.backup-codex-fix-20260805-1535}"
APP_DIR="/opt/qqbot"
NAPCAT_CONFIG_DIR="/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config"
test -d "$BACKUP_DIR"
if [[ -z "${NAPCAT_QUICK_ACCOUNT:-}" && -f /etc/napcat.env ]]; then
    # shellcheck disable=SC1091
    source /etc/napcat.env
fi
if [[ ${NAPCAT_QUICK_ACCOUNT:-} =~ ^[0-9]+$ ]]; then
    NAPCAT_ACCOUNT_CONFIG="napcat_${NAPCAT_QUICK_ACCOUNT}.json"
else
    NAPCAT_ACCOUNT_CONFIG="$(find "$BACKUP_DIR/napcat-config" -maxdepth 1 -type f -name 'napcat_[0-9]*.json' -printf '%f\n' | head -1)"
fi
test -n "$NAPCAT_ACCOUNT_CONFIG"
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
