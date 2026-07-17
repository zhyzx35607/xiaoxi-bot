"""Bounded event automation policy without timers or background services."""

import time

_LAST_EVENT = {}


def automation_enabled(config, name, default=True):
    return bool(config.get("automation", {}).get(name, default))


def allow_event(name, key, cooldown):
    now = time.time()
    token = (name, str(key))
    last = _LAST_EVENT.get(token, 0)
    if now - last < cooldown:
        return False
    _LAST_EVENT[token] = now
    if len(_LAST_EVENT) > 1000:
        cutoff = now - 86400
        for item, ts in list(_LAST_EVENT.items()):
            if ts < cutoff:
                _LAST_EVENT.pop(item, None)
    return True
