"""Occasional short AI voice replies with persistent per-group limits."""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime

from ..permission import get_group_config
from ..utils import atomic_write_json, bot_timezone, configured_timezone_name

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STATE_PATH = os.path.join(_ROOT, "data", "voice_reply_state.json")
_state = None


def _today(tz_name="Asia/Shanghai"):
    return datetime.now(bot_timezone(tz_name)).strftime("%Y-%m-%d")


def _load_state():
    global _state
    if _state is not None:
        return _state
    try:
        with open(_STATE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    groups = data.get("groups", {})
    _state = {"groups": groups if isinstance(groups, dict) else {}}
    return _state


def _save_state():
    if _state is None:
        return
    try:
        atomic_write_json(_STATE_PATH, _state, indent=2)
    except Exception as error:
        log.warning("Voice reply state save failed: %s", error)


def _settings(dispatcher):
    config = dispatcher.config.get("voice_reply", {})
    if not isinstance(config, dict):
        config = {}
    return {
        "enabled": bool(config.get("enabled", False)),
        "probability": max(0.0, min(1.0, float(config.get("probability", 0.08) or 0))),
        "min_chars": max(1, int(config.get("min_chars", 5) or 5)),
        "max_chars": max(5, int(config.get("max_chars", 45) or 45)),
        "cooldown_seconds": max(60, int(config.get("cooldown_seconds", 3600) or 3600)),
        "daily_limit": max(0, int(config.get("daily_limit", 2) or 0)),
        "character_id": str(config.get("character_id") or "lucy-voice-xueling"),
    }


def _eligible_text(text, settings):
    value = str(text or "").strip()
    if not settings["min_chars"] <= len(value) <= settings["max_chars"]:
        return False
    if "\n" in value or "\r" in value:
        return False
    if re.search(r"https?://|www\.|\x60{3}|[@#]\d{4,}", value, re.I):
        return False
    return True


async def maybe_send_short_voice(dispatcher, group_id, text):
    """Send a voice reply when eligible; return True only after confirmed success."""
    if not group_id:
        return False
    settings = _settings(dispatcher)
    if not settings["enabled"] or settings["daily_limit"] <= 0:
        return False
    group_config = get_group_config(dispatcher, group_id)
    if not group_config.get("features", {}).get("voice_reply", False):
        return False
    if not _eligible_text(text, settings):
        return False
    lock = getattr(dispatcher, "_voice_reply_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        dispatcher._voice_reply_lock = lock
    async with lock:
        now = time.time()
        tz_name = configured_timezone_name(getattr(dispatcher, "config", None))
        state = _load_state()
        group_key = str(group_id)
        record = state["groups"].get(group_key, {})
        if record.get("date") != _today(tz_name):
            record = {"date": _today(tz_name), "count": 0, "last_sent": 0}
        if int(record.get("count", 0) or 0) >= settings["daily_limit"]:
            return False
        if now - float(record.get("last_sent", 0) or 0) < settings["cooldown_seconds"]:
            return False
        if random.random() >= settings["probability"]:
            return False
        try:
            result = await dispatcher.client.send_group_ai_record(
                int(group_id), settings["character_id"], str(text).strip())
        except Exception as error:
            log.info("Short voice reply failed group=%s: %s", group_id, error)
            return False
        if (not isinstance(result, dict) or result.get("status") != "ok"
                or result.get("retcode", 0) != 0):
            log.info("Short voice reply rejected group=%s status=%s retcode=%s",
                     group_id, (result or {}).get("status") if isinstance(result, dict) else result,
                     (result or {}).get("retcode") if isinstance(result, dict) else None)
            return False
        record.update({"date": _today(tz_name),
                       "count": int(record.get("count", 0) or 0) + 1,
                       "last_sent": now})
        state["groups"][group_key] = record
        _save_state()
        log.info("Short voice reply sent group=%s chars=%d daily=%d/%d",
                 group_id, len(str(text).strip()), record["count"], settings["daily_limit"])
        return True


def reset_state_for_test():
    global _state
    _state = None
