#!/usr/bin/env python3
"""Disable NapCat console/file message logs without exposing config secrets."""

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path


def update_config(path):
    path = Path(path)
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("NapCat config root must be an object")

    desired = {
        "consoleLog": False,
        "consoleLogLevel": "warn",
        "fileLog": False,
        "fileLogLevel": "warn",
    }
    if all(config.get(key) == value for key, value in desired.items()):
        return False

    config.update(desired)
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=".napcat-log-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-dir",
        default="/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config",
    )
    parser.add_argument("--account", default=os.getenv("NAPCAT_QUICK_ACCOUNT", ""))
    args = parser.parse_args()

    names = ["napcat.json"]
    if str(args.account).isdigit():
        names.append("napcat_{}.json".format(args.account))
    changed = 0
    for name in names:
        path = Path(args.config_dir) / name
        if path.is_file():
            changed += int(update_config(path))
    print("updated {} NapCat logging config file(s)".format(changed))


if __name__ == "__main__":
    main()
