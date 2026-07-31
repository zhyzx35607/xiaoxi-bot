"""bot/uapi.py - uapis.cn API client with daily/monthly credit budget.

Budget policy (config "uapi" section, state in data/uapi_state.json):
  - daily_limit (default 100) split into user bucket (daily_limit - reserve)
    and auto bucket (reserve) for scheduled/background tasks.
  - month_limit (default 3400) leaves headroom under the 3500 free quota.
  - Budget exhaustion is SILENT for auto tasks (log only); commands check
    credits_available() first so they can reply "额度用完".
"""

import json
import logging
import os
import time

import aiohttp

from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_PATH = os.path.join(_ROOT, "data", "uapi_state.json")

BASE_URL = "https://uapis.cn/api/v1"

# Best-effort per-endpoint credit costs (uapis.cn pricing tiers).
ENDPOINT_COSTS = {
    "/misc/weather": 2,
    "/misc/hotboard": 2,
    "/saying/random": 1,
    "/answerbook/ask": 1,
    "/daily/news-image": 1,
    "/image/bing-daily": 1,
    "/game/epic-free": 1,
    "/random/image": 0,
    "/social/bilibili/videoinfo": 4,
    "/social/bilibili/archives": 4,
    "/search/aggregate": 2,
    "/translate/text": 1,
}

# Endpoints whose responses may be cached for CACHE_TTL seconds.
CACHEABLE = {"/misc/weather", "/misc/hotboard", "/game/epic-free"}
CACHE_TTL = 600

_cache = {}   # (path, key) -> (timestamp, data)
_state = None
_missing_key_log_ts = {}
_MISSING_KEY_LOG_INTERVAL = 3600


def _log_missing_key(path):
    now = time.monotonic()
    last_logged = _missing_key_log_ts.get(path, 0)
    if now - last_logged >= _MISSING_KEY_LOG_INTERVAL:
        _missing_key_log_ts[path] = now
        log.warning("uapi: no api key configured, skip %s", path)
    else:
        log.debug("uapi: no api key configured, skip %s", path)


def _today():
    return time.strftime("%Y-%m-%d")


def _month():
    return time.strftime("%Y-%m")


def _load_state():
    global _state
    if _state is not None:
        return _state
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _state = {
        "date": data.get("date", ""),
        "month": data.get("month", ""),
        "day_user": int(data.get("day_user", 0) or 0),
        "day_auto": int(data.get("day_auto", 0) or 0),
        "month_used": int(data.get("month_used", 0) or 0),
    }
    return _state


def _save_state():
    if _state is None:
        return
    try:
        atomic_write_json(_STATE_PATH, _state, indent=2)
    except Exception as e:
        log.warning("uapi state save failed: %s", e)


def _rollover(state):
    today, month = _today(), _month()
    changed = False
    if state["date"] != today:
        state["date"] = today
        state["day_user"] = 0
        state["day_auto"] = 0
        changed = True
    if state["month"] != month:
        state["month"] = month
        state["month_used"] = 0
        changed = True
    return changed


def _limits(config):
    uapi_cfg = config.get("uapi", {}) if isinstance(config.get("uapi"), dict) else {}
    daily = int(uapi_cfg.get("daily_limit", 100) or 100)
    reserve = int(uapi_cfg.get("reserve", 30) or 30)
    month_limit = int(uapi_cfg.get("month_limit", 3400) or 3400)
    return daily, reserve, month_limit


def credits_remaining(config):
    """Return remaining credits per bucket (for /积分 status display)."""
    state = _load_state()
    _rollover(state)
    daily, reserve, month_limit = _limits(config)
    user_cap = max(0, daily - reserve)
    return {
        "user_left": max(0, user_cap - state["day_user"]),
        "user_cap": user_cap,
        "auto_left": max(0, reserve - state["day_auto"]),
        "auto_cap": reserve,
        "month_left": max(0, month_limit - state["month_used"]),
        "month_cap": month_limit,
        "day_used": state["day_user"] + state["day_auto"],
    }


def credits_available(config, kind="user"):
    """Cheap pre-check so commands can reply "额度用完" before calling."""
    state = _load_state()
    _rollover(state)
    daily, reserve, month_limit = _limits(config)
    if state["month_used"] >= month_limit:
        return False
    if kind == "auto":
        return state["day_auto"] < reserve and (state["day_user"] + state["day_auto"]) < daily
    return state["day_user"] < max(0, daily - reserve)


def _charge(config, path, kind):
    """Authoritative charge; returns True if the call may proceed."""
    state = _load_state()
    if _rollover(state):
        _save_state()
    cost = ENDPOINT_COSTS.get(path, 2)
    if cost <= 0:
        return True  # free endpoints are never budget-blocked
    if not credits_available(config, kind):
        return False
    if kind == "auto":
        state["day_auto"] += cost
    else:
        state["day_user"] += cost
    state["month_used"] += cost
    _save_state()
    return True


def _cache_key(path, params):
    if not params:
        return (path, "")
    return (path, "&".join("{}={}".format(k, params[k]) for k in sorted(params)))


def _cache_get(path, params):
    if path not in CACHEABLE:
        return None
    item = _cache.get(_cache_key(path, params))
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    return None


def _cache_put(path, params, data):
    if path not in CACHEABLE or data is None:
        return
    if len(_cache) > 200:
        cutoff = time.time() - CACHE_TTL
        for key, (ts, _) in list(_cache.items()):
            if ts < cutoff:
                _cache.pop(key, None)
    _cache[_cache_key(path, params)] = (time.time(), data)


def _api_key(config):
    return str(config.get("uapi_api_key") or "").strip()


async def uapi_get(dispatcher, path, params=None, kind="user", timeout=8):
    """GET a JSON endpoint. Returns parsed data (dict/list) or None.

    Silent on budget exhaustion and network/API failure (log only).
    """
    cached = _cache_get(path, params)
    if cached is not None:
        return cached
    key = _api_key(dispatcher.config)
    if not key:
        _log_missing_key(path)
        return None
    if not _charge(dispatcher.config, path, kind):
        log.info("uapi: budget blocked %s kind=%s", path, kind)
        return None
    try:
        session = dispatcher.client.session
        headers = {"Authorization": "Bearer " + key}
        async with session.get(BASE_URL + path, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                log.warning("uapi %s -> HTTP %s", path, resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception as e:
        log.warning("uapi %s failed: %s", path, e)
        return None
    _cache_put(path, params, data)
    return data


async def uapi_post(dispatcher, path, json_body=None, kind="user", timeout=8):
    """POST a JSON endpoint. Returns parsed data or None.

    Budgeted through the same credit channel as uapi_get; silent on failure.
    """
    key = _api_key(dispatcher.config)
    if not key:
        _log_missing_key(path)
        return None
    if not _charge(dispatcher.config, path, kind):
        log.info("uapi: budget blocked %s kind=%s", path, kind)
        return None
    try:
        session = dispatcher.client.session
        headers = {"Authorization": "Bearer " + key}
        async with session.post(BASE_URL + path, json=json_body or {}, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                log.warning("uapi %s -> HTTP %s", path, resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception as e:
        log.warning("uapi %s failed: %s", path, e)
        return None
    return data


async def uapi_get_binary(dispatcher, path, params=None, kind="user",
                          max_bytes=6 * 1024 * 1024, timeout=20):
    """GET a binary (image) endpoint. Returns (bytes, content_type) or None."""
    key = _api_key(dispatcher.config)
    if not key:
        _log_missing_key(path)
        return None
    if not _charge(dispatcher.config, path, kind):
        log.info("uapi: budget blocked %s kind=%s", path, kind)
        return None
    try:
        session = dispatcher.client.session
        headers = {"Authorization": "Bearer " + key}
        async with session.get(BASE_URL + path, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                log.warning("uapi %s -> HTTP %s", path, resp.status)
                return None
            ctype = resp.headers.get("Content-Type", "image/jpeg")
            chunks = []
            total = 0
            async for chunk in resp.content.iter_chunked(65536):
                total += len(chunk)
                if total > max_bytes:
                    log.warning("uapi %s exceeded %d bytes, abort", path, max_bytes)
                    return None
                chunks.append(chunk)
            return b"".join(chunks), ctype
    except Exception as e:
        log.warning("uapi %s failed: %s", path, e)
        return None


async def uapi_resolve_image_url(dispatcher, path, params=None, timeout=8):
    """Resolve a 302-redirect image endpoint to its final URL (free endpoints).

    Returns the Location URL string or None. Never downloads the image itself,
    so NapCat fetches it directly and the bot uses no extra memory.
    """
    key = _api_key(dispatcher.config)
    if not key:
        _log_missing_key(path)
        return None
    if not _charge(dispatcher.config, path, "user"):
        log.info("uapi: budget blocked %s kind=free", path)
        return None
    try:
        session = dispatcher.client.session
        headers = {"Authorization": "Bearer " + key}
        async with session.get(BASE_URL + path, params=params, headers=headers,
                               allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                return resp.headers.get("Location")
            ctype = resp.headers.get("Content-Type", "")
            if resp.status == 200 and ctype.startswith("image/"):
                # Endpoint returned the image directly; hand the API URL to
                # NapCat would lose the auth header, so download is required.
                log.info("uapi %s returned image directly (no redirect)", path)
                return None
            log.warning("uapi %s -> HTTP %s", path, resp.status)
            return None
    except Exception as e:
        log.warning("uapi %s failed: %s", path, e)
        return None


def reset_state_for_test():
    """Test hook: drop cached budget state."""
    global _state
    _state = None
    _cache.clear()
    _missing_key_log_ts.clear()
