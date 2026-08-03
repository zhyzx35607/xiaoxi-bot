#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

project_root="${1:-/opt/qqbot}"
service_source="${project_root}/deploy/qqbot.service"
config_source="${project_root}/config.json"

id -u qqbot >/dev/null 2>&1 || useradd --system --home-dir /var/lib/qqbot --shell /usr/sbin/nologin qqbot
install -d -m 0700 -o qqbot -g qqbot /var/lib/qqbot /var/log/qqbot /run/qqbot
install -d -m 0700 -o qqbot -g qqbot "${project_root}/data"

while IFS= read -r -d '' file; do
  chmod 0644 "${project_root}/${file}"
done < <(git -C "${project_root}" ls-files -z)
chmod 0755 "${project_root}/deploy/install-qqbot-service.sh" \
  "${project_root}/scripts/migrate_runtime_config.py"
for directory in app bot deploy scripts tests docs .github; do
  if [[ -d "${project_root}/${directory}" ]]; then
    find "${project_root}/${directory}" -type d -exec chmod 0755 {} +
  fi
done

"${project_root}/venv/bin/python" "${project_root}/scripts/migrate_runtime_config.py" \
  --source "${config_source}" \
  --target /var/lib/qqbot/config.json \
  --environment-file /etc/qqbot.env
chown qqbot:qqbot /var/lib/qqbot/config.json /var/lib/qqbot/config.json.last-good
chmod 0600 /var/lib/qqbot/config.json /var/lib/qqbot/config.json.last-good
chown -R qqbot:qqbot "${project_root}/data"

install -m 0644 "${service_source}" /etc/systemd/system/qqbot.service
systemctl daemon-reload
systemctl restart qqbot.service
systemctl --no-pager --full status qqbot.service
