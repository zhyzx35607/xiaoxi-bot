#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

project_root="${1:-/opt/qqbot}"
service_source="${project_root}/deploy/qqbot.service"
config_source="${project_root}/config.json"

if [[ -d "${project_root}/.git" ]] && [[ "${QQBOT_ALLOW_DIRTY_DEPLOY:-0}" != "1" ]] && [[ -n "$(git -C "${project_root}" status --porcelain --untracked-files=all)" ]]; then
  echo "refusing to deploy a dirty worktree; set QQBOT_ALLOW_DIRTY_DEPLOY=1 only for an audited migration" >&2
  exit 1
fi

qqbot_was_active=0
watchdog_timer_was_active=0
watchdog_service_was_active=0
restore_runtime_services_on_exit=1
restore_runtime_services() {
  status=$?
  if [[ ${restore_runtime_services_on_exit} == 1 ]]; then
    if [[ ${watchdog_service_was_active} == 1 ]]; then
      systemctl start napcat-login-watchdog.service || true
    elif [[ ${watchdog_timer_was_active} == 1 ]]; then
      systemctl start napcat-login-watchdog.timer || true
    fi
    if [[ ${qqbot_was_active} == 1 ]]; then
      systemctl start qqbot.service || true
    fi
  fi
  exit "${status}"
}
trap restore_runtime_services EXIT

if systemctl is-active --quiet napcat-login-watchdog.timer; then
  watchdog_timer_was_active=1
  systemctl stop napcat-login-watchdog.timer
fi
if systemctl is-active --quiet napcat-login-watchdog.service; then
  watchdog_service_was_active=1
  systemctl stop napcat-login-watchdog.service
fi
if systemctl is-active --quiet qqbot.service; then
  qqbot_was_active=1
  systemctl stop qqbot.service
fi

id -u qqbot >/dev/null 2>&1 || useradd --system --home-dir /var/lib/qqbot --shell /usr/sbin/nologin qqbot
install -d -m 0700 -o qqbot -g qqbot /var/lib/qqbot /var/log/qqbot /run/qqbot
install -d -m 0700 -o qqbot -g qqbot \
  "${project_root}/data/tmp" \
  "${project_root}/data/diagnostics"
chown root:root "${project_root}"
chmod 0755 "${project_root}"

while IFS= read -r -d '' directory; do
  chown root:root "${directory}"
  chmod 0755 "${directory}"
done < <(
  find "${project_root}" -xdev \( -path "${project_root}/data" -o -path "${project_root}/data/*" -o -path "${project_root}/venv" -o -path "${project_root}/venv/*" -o -path "${project_root}/.git" -o -path "${project_root}/.git/*" \) -prune -o -type d -print0
)
while IFS= read -r -d '' file; do
  chown root:root "${file}"
  chmod 0644 "${file}"
done < <(
  find "${project_root}" -xdev \( -path "${project_root}/data" -o -path "${project_root}/data/*" -o -path "${project_root}/venv" -o -path "${project_root}/venv/*" -o -path "${project_root}/.git" -o -path "${project_root}/.git/*" \) -prune -o -type f -print0
)
for directory in deploy scripts; do
  if [[ -d "${project_root}/${directory}" ]]; then
    find "${project_root}/${directory}" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod 0755 {} +
  fi
done
chmod 0755 "${project_root}/deploy/prune_qqbot_backups.py"
find "${project_root}" -maxdepth 1 -type f \( -name 'config.json' -o -name 'config.json.*' \) -exec chmod 0600 {} +
unsafe_path="$(
  find "${project_root}" -xdev \( -path "${project_root}/data" -o -path "${project_root}/data/*" -o -path "${project_root}/venv" -o -path "${project_root}/venv/*" -o -path "${project_root}/.git" -o -path "${project_root}/.git/*" \) -prune -o \( -type d -o -type f \) -perm /022 -print -quit
)"
if [[ -n "${unsafe_path}" ]]; then
  echo "refusing unsafe project permissions: ${unsafe_path} is group/world writable" >&2
  exit 1
fi
if [[ -d "${project_root}/.git" ]]; then
  chown -R root:root "${project_root}/.git"
fi
if [[ -d "${project_root}/venv" ]]; then
  chown -R root:root "${project_root}/venv"
fi

"${project_root}/venv/bin/python" "${project_root}/scripts/migrate_runtime_config.py" \
  --source "${config_source}" \
  --target /var/lib/qqbot/config.json \
  --environment-file /etc/qqbot.env \
  --owner-user qqbot \
  --owner-group qqbot
chown qqbot:qqbot /var/lib/qqbot/config.json /var/lib/qqbot/config.json.last-good
chmod 0600 /var/lib/qqbot/config.json /var/lib/qqbot/config.json.last-good
chown -R qqbot:qqbot "${project_root}/data"
find "${project_root}/data" -type d -exec chmod 0700 {} +
find "${project_root}/data" -type f -exec chmod 0600 {} +
install -d -m 0755 /etc/systemd/system/qqbot.service.d
rm -f /etc/systemd/system/qqbot.service.d/20-security.conf
rmdir /etc/systemd/system/qqbot.service.d 2>/dev/null || true
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "${project_root}/deploy/qqbot-journald.conf" /etc/systemd/journald.conf.d/30-qqbot.conf
systemctl restart systemd-journald.service
install -m 0644 "${service_source}" /etc/systemd/system/qqbot.service
install -m 0644 "${project_root}/deploy/napcat-login-watchdog.service" /etc/systemd/system/napcat-login-watchdog.service
install -m 0644 "${project_root}/deploy/qqbot-backup-prune.service" /etc/systemd/system/qqbot-backup-prune.service
install -m 0644 "${project_root}/deploy/qqbot-backup-prune.timer" /etc/systemd/system/qqbot-backup-prune.timer
systemctl daemon-reload
systemctl enable --now napcat-login-watchdog.service
systemctl enable --now qqbot-backup-prune.timer
if [[ ${QQBOT_SKIP_RESTART:-0} != 1 ]]; then
  systemctl restart qqbot.service
  systemctl --no-pager --full status qqbot.service
elif [[ ${qqbot_was_active} == 1 ]]; then
  systemctl start qqbot.service
fi
systemctl disable --now napcat-login-watchdog.timer 2>/dev/null || true
rm -f /etc/systemd/system/napcat-login-watchdog.timer
systemctl daemon-reload
restore_runtime_services_on_exit=0
trap - EXIT
