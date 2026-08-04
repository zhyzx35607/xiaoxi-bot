"""Quota and cooldown accounting for future proactive Agent messages."""

import time
from datetime import datetime

from .policy import agent_config, is_quiet_hours


class ProactiveBudget:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return f"proactive/{scope_key.replace(':', '_')}.json"

    def _state(self, scope_key):
        state = self.store.read(self._path(scope_key), {"day": "", "sent": [], "topics": {}, "muted_until": 0})
        return state if isinstance(state, dict) else {"day": "", "sent": [], "topics": {}, "muted_until": 0}

    def allowed(self, config, scope_key, *, topic="", is_private=False, now=None):
        now = now or time.time()
        settings = agent_config(config)
        if is_quiet_hours(settings, datetime.fromtimestamp(now)):
            return False, "quiet_hours"
        state = self._state(scope_key)
        if float(state.get("muted_until", 0) or 0) > now:
            return False, "muted"
        day = datetime.fromtimestamp(now).strftime("%Y%m%d")
        sent = state.get("sent", []) if state.get("day") == day else []
        limit = int(settings["owner_daily_limit"] if is_private else settings["group_daily_limit"])
        if len(sent) >= limit:
            return False, "daily_limit"
        last_topic = float((state.get("topics") or {}).get(topic, 0) or 0) if topic else 0
        if topic and now - last_topic < int(settings["topic_cooldown_seconds"]):
            return False, "topic_cooldown"
        return True, "ok"

    def record(self, config, scope_key, *, topic="", now=None):
        now = now or time.time(); state = self._state(scope_key)
        day = datetime.fromtimestamp(now).strftime("%Y%m%d")
        if state.get("day") != day: state = {"day": day, "sent": [], "topics": {}, "muted_until": 0}
        state.setdefault("sent", []).append(now)
        if topic: state.setdefault("topics", {})[topic] = now
        self.store.write(self._path(scope_key), state)

    def mute(self, scope_key, seconds=43200, now=None):
        state = self._state(scope_key); state["muted_until"] = (now or time.time()) + seconds
        self.store.write(self._path(scope_key), state)
