"""Runtime-only filesystem paths."""

import os
import tempfile
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def runtime_temp_dir():
    configured = os.getenv("QQBOT_TMP_DIR")
    path = Path(configured) if configured else _PROJECT_ROOT / "data" / "tmp"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return str(path)


def create_runtime_temp_file(prefix, suffix):
    fd, path = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=runtime_temp_dir(),
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return fd, path