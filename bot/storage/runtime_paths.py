"""Runtime-only filesystem paths."""

import logging
import os
import tempfile
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("qqbot")


def _prepare_private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    if not os.access(path, os.W_OK | os.X_OK):
        raise PermissionError("runtime directory is not writable: {}".format(path))
    return path


def _runtime_directory(environment_name, fallback):
    configured = os.getenv(environment_name)
    candidates = []
    if configured:
        candidates.append(Path(configured))
    if not candidates or candidates[0] != fallback:
        candidates.append(fallback)
    errors = []
    for index, path in enumerate(candidates):
        try:
            return str(_prepare_private_directory(path))
        except OSError as error:
            errors.append("{}: {}".format(path, error))
            if index + 1 < len(candidates):
                log.warning(
                    "%s is unavailable; falling back to %s",
                    environment_name, candidates[index + 1],
                )
    raise RuntimeError(
        "no writable runtime directory for {} ({})".format(
            environment_name, "; ".join(errors))
    )


def runtime_temp_dir():
    return _runtime_directory("QQBOT_TMP_DIR", _PROJECT_ROOT / "data" / "tmp")


def runtime_diagnostics_dir():
    return _runtime_directory(
        "QQBOT_DIAGNOSTICS_DIR", _PROJECT_ROOT / "data" / "diagnostics")


def runtime_diagnostic_path(filename):
    safe_name = Path(str(filename)).name
    if not safe_name:
        raise ValueError("diagnostic filename is required")
    return str(Path(runtime_diagnostics_dir()) / safe_name)


def create_runtime_temp_file(prefix, suffix, world_readable=False):
    fd, path = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=runtime_temp_dir(),
    )
    try:
        # world_readable is only for non-sensitive media/text handed to the
        # separate NapCat process (napcat user); everything else stays 0600.
        os.chmod(path, 0o644 if world_readable else 0o600)
    except OSError:
        pass
    return fd, path
