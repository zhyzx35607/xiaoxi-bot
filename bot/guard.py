"""bot/guard.py - Content protection with blacklist system"""
import json, os, time, logging
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLACKLIST_FILE = os.path.join(_ROOT, "data", "blacklist.json")
R18_WARNING_FILE = os.path.join(_ROOT, "data", "r18_warnings.json")

# In-memory cache to avoid repeated disk I/O (TTL 30s)
_bl_cache = None
_bl_cache_ts = 0
_warn_cache = None
_warn_cache_ts = 0
_CACHE_TTL = 30


def load_blacklist():
    global _bl_cache, _bl_cache_ts
    now = time.time()
    if _bl_cache is not None and now - _bl_cache_ts < _CACHE_TTL:
        return _bl_cache
    try:
        with open(BLACKLIST_FILE, encoding="utf-8") as f:
            _bl_cache = json.load(f)
    except Exception:
        _bl_cache = {}
    _bl_cache_ts = now
    return _bl_cache


def save_blacklist(bl):
    global _bl_cache, _bl_cache_ts
    _bl_cache = bl
    _bl_cache_ts = time.time()
    atomic_write_json(BLACKLIST_FILE, bl, indent=2)


def load_warnings():
    global _warn_cache, _warn_cache_ts
    now = time.time()
    if _warn_cache is not None and now - _warn_cache_ts < _CACHE_TTL:
        return _warn_cache
    try:
        with open(R18_WARNING_FILE, encoding="utf-8") as f:
            _warn_cache = json.load(f)
    except Exception:
        _warn_cache = {}
    _warn_cache_ts = now
    return _warn_cache


def save_warnings(w):
    global _warn_cache, _warn_cache_ts
    _warn_cache = w
    _warn_cache_ts = time.time()
    atomic_write_json(R18_WARNING_FILE, w, indent=2)


def is_blacklisted(group_id, user_id):
    bl = load_blacklist()
    key = f"{group_id}_{user_id}"
    entry = bl.get(key)
    if entry and time.time() < entry.get("expires", 0):
        return True
    return False


def add_blacklist(group_id, user_id, duration_hours=48, bot_owner=None, bot_qq=None):
    # Never blacklist the bot owner or bot itself
    if bot_owner is None or bot_qq is None:
        import json as _json, os as _os
        try:
            cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "config.json")
            with open(cfg_path) as _f: cfg = _json.load(_f)
            if bot_owner is None:
                bot_owner = cfg.get("bot_owner")
            if bot_qq is None:
                bot_qq = cfg.get("bot_qq")
        except Exception:
            pass
    if user_id == bot_owner or user_id == bot_qq:
        log.info("Skipped blacklist for bot owner/self: %s", user_id)
        return

    bl = load_blacklist()
    key = f"{group_id}_{user_id}"
    bl[key] = {
        "group_id": group_id,
        "user_id": user_id,
        "added": time.time(),
        "expires": time.time() + duration_hours * 3600
    }
    save_blacklist(bl)
    log.info("Blacklisted user %s in group %s for %sh", user_id, group_id, duration_hours)


def remove_blacklist(group_id, user_id):
    bl = load_blacklist()
    key = f"{group_id}_{user_id}"
    if key in bl:
        del bl[key]
        save_blacklist(bl)


def get_warning_count(group_id, user_id, window_hours=2):
    warnings = load_warnings()
    key = f"{group_id}_{user_id}"
    entries = warnings.get(key, [])
    cutoff = time.time() - window_hours * 3600
    return sum(1 for t in entries if t > cutoff)


def add_warning(group_id, user_id):
    warnings = load_warnings()
    key = f"{group_id}_{user_id}"
    if key not in warnings:
        warnings[key] = []
    warnings[key].append(time.time())
    cutoff = time.time() - 86400
    warnings[key] = [t for t in warnings[key] if t > cutoff]
    save_warnings(warnings)
    log.info("R18 warning for user %s in group %s (count: %d)", user_id, group_id, len(warnings[key]))
