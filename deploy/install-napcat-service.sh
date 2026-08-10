#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

project_root="${1:-/opt/qqbot}"
environment_file=/etc/napcat.env
napcat_user=napcat
napcat_home=/var/lib/napcat
napcat_root=/opt/napcat
legacy_napcat_root=/root/Napcat
legacy_qq_config=/root/.config/QQ
napcat_config_dir="${napcat_root}/opt/QQ/resources/app/app_launcher/napcat/config"
napcat_was_active=0

restore_napcat_on_failure() {
  status=$?
  trap - EXIT
  if [[ ${status} -ne 0 && ${napcat_was_active} == 1 ]]; then
    systemctl daemon-reload || true
    systemctl start napcat.service || true
  fi
  exit "${status}"
}

trap restore_napcat_on_failure EXIT

if [[ ! -f "${environment_file}" ]]; then
  echo "missing ${environment_file}" >&2
  exit 1
fi
chown root:root "${environment_file}"
chmod 0600 "${environment_file}"

napcat_account="$(python3 - "${environment_file}" <<'PY'
import sys

path = sys.argv[1]
value = ""
with open(path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() != "NAPCAT_QUICK_ACCOUNT":
            continue
        candidate = candidate.strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
            candidate = candidate[1:-1]
        value = candidate
print(value)
PY
)"
if [[ ! ${napcat_account} =~ ^[0-9]+$ ]]; then
  echo "NAPCAT_QUICK_ACCOUNT must be present in ${environment_file}" >&2
  exit 1
fi

if [[ ! -x "${legacy_napcat_root}/opt/QQ/qq" && ! -x "${napcat_root}/opt/QQ/qq" ]]; then
  echo "NapCat QQ binary was not found" >&2
  exit 1
fi

if ! id -u "${napcat_user}" >/dev/null 2>&1; then
  useradd --system --home-dir "${napcat_home}" --create-home \
    --shell /usr/sbin/nologin "${napcat_user}"
fi
install -d -o "${napcat_user}" -g "${napcat_user}" -m 0700 \
  "${napcat_home}" "${napcat_home}/.config" "${napcat_home}/.cache" \
  "${napcat_home}/.local" "${napcat_home}/.local/share"

if systemctl is-active --quiet napcat.service; then
  napcat_was_active=1
  systemctl stop napcat.service
fi

if [[ ! -x "${napcat_root}/opt/QQ/qq" ]]; then
  install -d -o "${napcat_user}" -g "${napcat_user}" -m 0750 "${napcat_root}"
  cp -a "${legacy_napcat_root}/." "${napcat_root}/"
fi
if [[ ! -d "${napcat_home}/.config/QQ" ]]; then
  if [[ ! -d "${legacy_qq_config}" ]]; then
    echo "legacy QQ profile was not found at ${legacy_qq_config}" >&2
    exit 1
  fi
  cp -a "${legacy_qq_config}" "${napcat_home}/.config/QQ"
fi
chown -R "${napcat_user}:${napcat_user}" "${napcat_root}" "${napcat_home}"
chmod 0700 "${napcat_home}/.config" "${napcat_home}/.config/QQ"

chmod 0755 \
  "${project_root}/deploy/napcat_log_filter.py" \
  "${project_root}/deploy/configure_napcat_logging.py" \
  "${project_root}/deploy/install-napcat-service.sh"
"${project_root}/deploy/configure_napcat_logging.py" \
  --config-dir "${napcat_config_dir}" --account "${napcat_account}"
if [[ -d "${napcat_config_dir}" ]]; then
  while IFS= read -r -d '' config_file; do
    chown "${napcat_user}:${napcat_user}" "${config_file}"
    chmod 0600 "${config_file}"
  done < <(find "${napcat_config_dir}" -maxdepth 1 -type f \( \
    -name 'napcat*.json' -o -name 'onebot11_*.json' \
  \) -print0)
fi
install -m 0644 "${project_root}/deploy/napcat.service" /etc/systemd/system/napcat.service
install -m 0644 "${project_root}/deploy/napcat-login-watchdog.service" /etc/systemd/system/napcat-login-watchdog.service
install -m 0644 "${project_root}/deploy/napcat-restart.service" /etc/systemd/system/napcat-restart.service
install -m 0644 "${project_root}/deploy/napcat-restart.path" /etc/systemd/system/napcat-restart.path
systemctl daemon-reload
systemctl enable --now napcat-restart.path
systemctl enable --now napcat-login-watchdog.service

if [[ ${NAPCAT_SKIP_RESTART:-0} != 1 ]]; then
  systemctl restart napcat.service
  systemctl --no-pager --full status napcat.service
elif [[ ${napcat_was_active} == 1 ]]; then
  systemctl start napcat.service
fi
trap - EXIT
