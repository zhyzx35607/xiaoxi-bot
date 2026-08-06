#!/usr/bin/env python3
"""Prune old top-level QQ Bot backup files with a newest-file safety floor."""

import argparse
import time
from pathlib import Path


def select_for_pruning(directory, keep=20, max_age_days=30, now=None):
    root = Path(directory)
    if not root.is_dir():
        return []
    files = sorted(
        (path for path in root.iterdir() if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    cutoff = (time.time() if now is None else float(now)) - max_age_days * 86400
    return [path for path in files[max(0, int(keep)):] if path.stat().st_mtime < cutoff]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="/root/qqbot-backups")
    parser.add_argument("--keep", type=int, default=20)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    selected = select_for_pruning(args.directory, args.keep, args.max_age_days)
    for path in selected:
        print(("remove" if args.apply else "would remove") + " " + path.name)
        if args.apply:
            path.unlink()
    print("{} backup file(s) selected".format(len(selected)))


if __name__ == "__main__":
    main()
