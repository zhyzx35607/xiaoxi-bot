"""bot/guard.py - Content protection with blacklist system"""
import asyncio, json, os, threading, time, logging
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
# Serializes cache read-modify-write between event-loop to_thread workers and
# synchronous callers. RLock because the public load/save helpers re-acquire
# it inside the mutating operations below.
_cache_lock = threading.RLock()


def load_blacklist():
    global _bl_cache, _bl_cache_ts
    with _cache_lock:
        now = time.time()
        if _bl_cache is not None and now - _bl_cache_ts < _CACHE_TTL:
            return _bl_cache
        try:
            with open(BLACKLIST_FILE, encoding="utf-8") as f:
                _bl_cache = json.load(f)
        except (OSError, ValueError):
            _bl_cache = {}
        _bl_cache_ts = now
        return _bl_cache


def save_blacklist(bl):
    global _bl_cache, _bl_cache_ts
    with _cache_lock:
        _bl_cache = bl
        _bl_cache_ts = time.time()
        atomic_write_json(BLACKLIST_FILE, bl, indent=2)


def load_warnings():
    global _warn_cache, _warn_cache_ts
    with _cache_lock:
        now = time.time()
        if _warn_cache is not None and now - _warn_cache_ts < _CACHE_TTL:
            return _warn_cache
        try:
            with open(R18_WARNING_FILE, encoding="utf-8") as f:
                _warn_cache = json.load(f)
        except (OSError, ValueError):
            _warn_cache = {}
        _warn_cache_ts = now
        return _warn_cache


def save_warnings(w):
    global _warn_cache, _warn_cache_ts
    with _cache_lock:
        _warn_cache = w
        _warn_cache_ts = time.time()
        atomic_write_json(R18_WARNING_FILE, w, indent=2)


def _is_blacklisted(group_id, user_id):
    with _cache_lock:
        bl = load_blacklist()
        key = f"{group_id}_{user_id}"
        entry = bl.get(key)
        if not entry:
            return False
        if time.time() < entry.get("expires", 0):
            return True
        # Lazily purge expired entries so blacklist.json does not grow forever.
        del bl[key]
        save_blacklist(bl)
        return False


async def is_blacklisted(group_id, user_id):
    # The cache-miss read and the expired-entry purge touch disk (fsync), so
    # the whole check runs in a worker thread instead of the event loop.
    return await asyncio.to_thread(_is_blacklisted, group_id, user_id)


def add_blacklist(group_id, user_id, duration_hours=48, bot_owner=None, bot_qq=None):
    # Never blacklist the bot owner or bot itself
    if bot_owner is None or bot_qq is None:
        import json as _json, os as _os
        try:
            cfg_path = (_os.getenv("QQBOT_CONFIG_PATH") or _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "config.json"))
            with open(cfg_path) as _f: cfg = _json.load(_f)
            if bot_owner is None:
                bot_owner = cfg.get("bot_owner")
            if bot_qq is None:
                bot_qq = cfg.get("bot_qq")
        except (OSError, _json.JSONDecodeError, TypeError, ValueError) as error:
            log.warning("Unable to read protected account IDs from config: %s", error)
    # user_id 可能来自事件字符串，与 config 的 int 比较前统一转换
    def _protected_id(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    protected = {_protected_id(bot_owner), _protected_id(bot_qq)} - {None}
    if _protected_id(user_id) in protected:
        log.info("Skipped blacklist for bot owner/self: %s", user_id)
        return

    key = f"{group_id}_{user_id}"
    with _cache_lock:
        bl = load_blacklist()
        bl[key] = {
            "group_id": group_id,
            "user_id": user_id,
            "added": time.time(),
            "expires": time.time() + duration_hours * 3600
        }
        save_blacklist(bl)
    log.info("Blacklisted user %s in group %s for %sh", user_id, group_id, duration_hours)


def remove_blacklist(group_id, user_id):
    with _cache_lock:
        bl = load_blacklist()
        key = f"{group_id}_{user_id}"
        if key in bl:
            del bl[key]
            save_blacklist(bl)


def get_warning_count(group_id, user_id, window_hours=2):
    with _cache_lock:
        warnings = load_warnings()
        key = f"{group_id}_{user_id}"
        entries = warnings.get(key, [])
        cutoff = time.time() - window_hours * 3600
        return sum(1 for t in entries if t > cutoff)


def add_warning(group_id, user_id):
    with _cache_lock:
        warnings = load_warnings()
        key = f"{group_id}_{user_id}"
        if key not in warnings:
            warnings[key] = []
        warnings[key].append(time.time())
        cutoff = time.time() - 86400
        warnings[key] = [t for t in warnings[key] if t > cutoff]
        save_warnings(warnings)
        count = len(warnings[key])
    log.info("R18 warning for user %s in group %s (count: %d)", user_id, group_id, count)
