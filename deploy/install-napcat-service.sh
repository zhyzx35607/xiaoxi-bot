#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

project_root="${1:-/opt/qqbot}"
environment_file=/etc/napcat.env

if [[ ! -f "${environment_file}" ]]; then
  echo "missing ${environment_file}" >&2
  exit 1
fi
chown root:root "${environment_file}"
chmod 0600 "${environment_file}"

set -a
# shellcheck disable=SC1090
source "${environment_file}"
set +a
if [[ ! ${NAPCAT_QUICK_ACCOUNT:-} =~ ^[0-9]+$ ]]; then
  echo "NAPCAT_QUICK_ACCOUNT must be present in ${environment_file}" >&2
  exit 1
fi

chmod 0755 \
  "${project_root}/deploy/napcat_log_filter.py" \
  "${project_root}/deploy/configure_napcat_logging.py" \
  "${project_root}/deploy/install-napcat-service.sh"
"${project_root}/deploy/configure_napcat_logging.py" --account "${NAPCAT_QUICK_ACCOUNT}"
install -m 0644 "${project_root}/deploy/napcat.service" /etc/systemd/system/napcat.service
systemctl daemon-reload

if [[ ${NAPCAT_SKIP_RESTART:-0} != 1 ]]; then
  systemctl restart napcat.service
  systemctl --no-pager --full status napcat.service
fi
