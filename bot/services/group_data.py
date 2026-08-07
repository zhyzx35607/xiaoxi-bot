"""Backup and clear group-scoped runtime data."""

import io
import json
import os
import secrets
import tarfile
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DATA_ROOT = _ROOT / "data"
_BACKUP_DIR = _DATA_ROOT / "operation_backups"
_LOCK = threading.RLock()
_ALL_SCOPES = frozenset(("memory", "stickers", "guard"))
_BACKUP_MAX_AGE = 30 * 86400
_BACKUP_KEEP = 10


def _normalize_scopes(scopes):
    normalized = frozenset(scopes or _ALL_SCOPES)
    if not normalized or not normalized.issubset(_ALL_SCOPES):
        raise ValueError("invalid clear scopes")
    return normalized


def _group_files(group_ids, scopes):
    paths = []
    memory_dir = _DATA_ROOT / "memories"
    sticker_dir = _DATA_ROOT / "stickers"
    for group_id in group_ids:
        if "memory" in scopes:
            paths.extend((
                memory_dir / "group_{}.json".format(group_id),
                memory_dir / "group_{}_long.json".format(group_id),
            ))
            paths.extend(memory_dir.glob("group_{}_u*.json".format(group_id)))
        if "stickers" in scopes:
            paths.append(sticker_dir / "group_{}.json".format(group_id))
    if "guard" in scopes:
        paths.extend((_DATA_ROOT / "blacklist.json", _DATA_ROOT / "r18_warnings.json"))
    return sorted({path for path in paths if path.is_file() and not path.is_symlink()}, key=str)


def _prune_old_backups(now=None):
    now = time.time() if now is None else now
    backups = sorted(
        (path for path in _BACKUP_DIR.glob("clearai-*.tar.gz")
         if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in backups[_BACKUP_KEEP:]:
        if now - path.stat().st_mtime > _BACKUP_MAX_AGE:
            path.unlink()


def _create_backup(group_ids, scopes):
    _BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        _BACKUP_DIR.chmod(0o700)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = "clearai-{}-{}.tar.gz".format(stamp, secrets.token_hex(4))
    final_path = _BACKUP_DIR / name
    fd, temporary = tempfile.mkstemp(prefix=".clearai-", suffix=".tar.gz", dir=_BACKUP_DIR)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        with tarfile.open(temporary_path, "w:gz") as archive:
            manifest = json.dumps({
                "operation": "clear_group_data",
                "group_ids": list(group_ids),
                "scopes": sorted(scopes),
                "created_at": time.time(),
            }, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(manifest))
            for path in _group_files(group_ids, scopes):
                archive.add(path, arcname=path.relative_to(_DATA_ROOT), recursive=False)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, final_path)
        _prune_old_backups()
        return final_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _remove_group_files(group_ids, scopes):
    removed = 0
    memory_dir = _DATA_ROOT / "memories"
    sticker_dir = _DATA_ROOT / "stickers"
    for group_id in group_ids:
        targets = []
        if "memory" in scopes:
            targets.extend((
                memory_dir / "group_{}.json".format(group_id),
                memory_dir / "group_{}_long.json".format(group_id),
            ))
            targets.extend(memory_dir.glob("group_{}_u*.json".format(group_id)))
        if "stickers" in scopes:
            targets.append(sticker_dir / "group_{}.json".format(group_id))
        for path in targets:
            if path.is_file():
                path.unlink()
                removed += 1
    return removed


def _clear_guard_entries(group_ids):
    from .. import guard

    prefixes = tuple("{}_".format(group_id) for group_id in group_ids)
    blacklist = dict(guard.load_blacklist())
    warnings = dict(guard.load_warnings())
    new_blacklist = {key: value for key, value in blacklist.items()
                     if not str(key).startswith(prefixes)}
    new_warnings = {key: value for key, value in warnings.items()
                    if not str(key).startswith(prefixes)}
    if new_blacklist != blacklist:
        guard.save_blacklist(new_blacklist)
    if new_warnings != warnings:
        guard.save_warnings(new_warnings)
    return len(blacklist) - len(new_blacklist) + len(warnings) - len(new_warnings)


def clear_group_data(dispatcher, group_ids, scopes=None):
    """Create a private rollback archive, then remove data for validated groups."""
    normalized = tuple(dict.fromkeys(str(value) for value in group_ids))
    if not normalized or any(not value.isdigit() for value in normalized):
        raise ValueError("invalid group targets")
    configured = {str(value) for value in dispatcher.config.get("groups", {})}
    if any(value not in configured for value in normalized):
        raise ValueError("group target is not configured")
    normalized_scopes = _normalize_scopes(scopes)
    with _LOCK:
        backup_path = _create_backup(normalized, normalized_scopes)
        removed = _remove_group_files(normalized, normalized_scopes)
        if "guard" in normalized_scopes:
            removed += _clear_guard_entries(normalized)
    return {
        "backup": str(backup_path),
        "backup_name": backup_path.name,
        "group_ids": list(normalized),
        "scopes": sorted(normalized_scopes),
        "removed": removed,
    }


def prepare_group_data_clear(group_ids, scopes=None):
    """Cancel in-flight memory work before disk cleanup runs in a thread."""
    from ..ai.memory import clear_group_memory_cache

    if "memory" in _normalize_scopes(scopes):
        for group_id in group_ids:
            clear_group_memory_cache(group_id)
