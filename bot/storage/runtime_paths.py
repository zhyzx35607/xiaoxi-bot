"""Runtime-only filesystem paths."""

import logging
import os
import tempfile
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("qqbot")


def _prepare_private_directory(path, mode=0o700):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass
    if not os.access(path, os.W_OK | os.X_OK):
        raise PermissionError("runtime directory is not writable: {}".format(path))
    return path


def _ensure_parent_traversable(path, mode=0o751):
    """Grant traverse-only (o+x) on the parent so the separate napcat user can
    reach shared media temp files without being able to list the parent."""
    parent = path.parent
    try:
        current = parent.stat().st_mode & 0o777
        parent.chmod(current | mode)
    except OSError:
        pass


def _runtime_directory(environment_name, fallback, mode=0o700):
    configured = os.getenv(environment_name)
    candidates = []
    if configured:
        candidates.append(Path(configured))
    if not candidates or candidates[0] != fallback:
        candidates.append(fallback)
    errors = []
    for index, path in enumerate(candidates):
        try:
            return str(_prepare_private_directory(path, mode))
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
    # data/tmp only holds shareable media temp files handed to the separate
    # NapCat process (napcat user) via file://, so it must be world-traversable;
    # everything sensitive stays in the other 0700 runtime directories.
    resolved = Path(_runtime_directory(
        "QQBOT_TMP_DIR", _PROJECT_ROOT / "data" / "tmp", mode=0o755))
    if resolved.parent == _PROJECT_ROOT / "data":
        _ensure_parent_traversable(resolved)
    return str(resolved)


def runtime_diagnostics_dir():
    return _runtime_directory(
        "QQBOT_DIAGNOSTICS_DIR", _PROJECT_ROOT / "data" / "diagnostics")


def runtime_data_dir(name):
    """Unified entry for persistent data subdirectories (memories, stickers...)."""
    safe = Path(str(name)).name
    if not safe:
        raise ValueError("data directory name is required")
    return str(_prepare_private_directory(_PROJECT_ROOT / "data" / safe))


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
