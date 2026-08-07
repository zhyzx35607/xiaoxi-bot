#!/usr/bin/env python3
"""Move runtime configuration to a private state directory without persisted secrets."""

import argparse
import json
import os
import shlex
import tempfile
from pathlib import Path

SECRET_ENV_KEYS = {
    "token": ("QQBOT_TOKEN", "QQBOT_ONEBOT_TOKEN", "ONEBOT_ACCESS_TOKEN"),
    "deepseek_api_key": ("DEEPSEEK_API_KEY", "QQBOT_DEEPSEEK_API_KEY"),
    "sigmai_api_key": ("SIGMAI_API_KEY", "QQBOT_SIGMAI_API_KEY"),
    "agnes_api_key": ("AGNES_API_KEY", "QQBOT_AGNES_API_KEY"),
    "uapi_api_key": ("UAPI_API_KEY", "QQBOT_UAPI_API_KEY"),
    "bili_sessdata": ("BILI_SESSDATA", "QQBOT_BILI_SESSDATA"),
    "touchgal_api_token": ("TOUCHGAL_API_TOKEN", "QQBOT_TOUCHGAL_API_TOKEN"),
}


def load_environment_file(path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        try:
            parts = shlex.split(raw_value, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = raw_value.strip().strip("\"'")
        values[key] = value
    return values


def remove_env_managed_secrets(config, environment):
    removed = []
    for config_key, env_names in SECRET_ENV_KEYS.items():
        if any(environment.get(name) for name in env_names) and config.pop(config_key, None) is not None:
            removed.append(config_key)
    vision = config.get("vision_api")
    if (isinstance(vision, dict)
            and (environment.get("VISION_API_KEY") or environment.get("QQBOT_VISION_API_KEY"))
            and vision.pop("api_key", None) is not None):
        removed.append("vision_api.api_key")
    return removed


def atomic_write(path, data, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if owner is not None:
            os.chown(temporary, owner[0], owner[1])
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--environment-file", type=Path, default=Path("/etc/qqbot.env"))
    parser.add_argument("--owner-user")
    parser.add_argument("--owner-group")
    args = parser.parse_args()

    owner = None
    if args.owner_user or args.owner_group:
        if not args.owner_user or not args.owner_group:
            raise SystemExit("--owner-user and --owner-group must be provided together")
        import grp
        import pwd

        owner = (
            pwd.getpwnam(args.owner_user).pw_uid,
            grp.getgrnam(args.owner_group).gr_gid,
        )

    source = args.target if args.target.exists() else args.source
    config = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise SystemExit("configuration root must be a JSON object")
    removed = remove_env_managed_secrets(config, load_environment_file(args.environment_file))
    atomic_write(args.target, config, owner=owner)
    atomic_write(Path(str(args.target) + ".last-good"), config, owner=owner)
    print("runtime configuration ready; removed: " + (", ".join(removed) or "none"))


if __name__ == "__main__":
    main()
