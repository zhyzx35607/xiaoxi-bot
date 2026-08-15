"""Compatibility wrapper for storage utilities plus shared timezone helpers."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage.json_store import atomic_write_json

__all__ = [
    "atomic_write_json",
    "bot_timezone",
    "configured_timezone_name",
    "now_in_timezone",
]

log = logging.getLogger("qqbot")
_TZ_WARNING_NAMES = set()


def bot_timezone(name="Asia/Shanghai"):
    """Return a tzinfo, falling back to fixed UTC+8 when tzdata is missing."""
    try:
        return ZoneInfo(str(name or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        key = str(name or "Asia/Shanghai")
        if key not in _TZ_WARNING_NAMES:
            _TZ_WARNING_NAMES.add(key)
            log.info("Timezone database unavailable for %s; using fixed UTC+8", name)
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def configured_timezone_name(config):
    runtime = (config or {}).get("runtime", {})
    return str(runtime.get("scheduler_timezone") or "Asia/Shanghai")


def now_in_timezone(config):
    """Current time in the configured scheduler timezone (Asia/Shanghai)."""
    return datetime.now(bot_timezone(configured_timezone_name(config)))
