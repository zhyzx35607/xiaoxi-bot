"""uapis.cn client with local task budgets and server-reported credit usage.

Local user/automation buckets prevent scheduled work from consuming the command
allowance. Official monthly quota and actual debits are learned from UApiS
response headers instead of being guessed before a request is sent.
"""

import asyncio
import json
import logging
import os
import re
import time

import aiohttp

from ..utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STATE_PATH = os.path.join(_ROOT, "data", "uapi_state.json")

BASE_URL = "https://uapis.cn/api/v1"

# Fallback prices from the official OpenAPI document. The response header
# Uapi-Credits-Charged remains authoritative because cached calls can cost less.
ENDPOINT_COSTS = {
    "/misc/weather": 2,
    "/misc/hotboard": 1,
    "/saying": 0,
    "/saying/random": 1,
    "/answerbook/ask": 0,
    "/daily/news-image": 1,
    "/image/bing-daily": 0,
    "/image/bing-daily/history": 0,
    "/game/epic-free": 1,
    "/random/image": 0,
    "/social/bilibili/videoinfo": 4,
    "/social/bilibili/archives": 4,
    "/search/aggregate": 4,
    "/translate/text": 2,
    "/status/usage": 0,
}

CACHEABLE = {"/misc/weather", "/misc/hotboard", "/game/epic-free"}
CACHE_TTL = 600

_cache = {}
_state = None
_missing_key_log_ts = {}
_MISSING_KEY_LOG_INTERVAL = 3600


def _log_missing_key(path):
    """Rate-limit the informational visitor-quota message."""
    now = time.monotonic()
    last_logged = _missing_key_log_ts.get(path, 0)
    if now - last_logged >= _MISSING_KEY_LOG_INTERVAL:
        _missing_key_log_ts[path] = now
        log.info("uapi: no api key configured, using visitor quota for %s", path)
    else:
        log.debug("uapi: using visitor quota for %s", path)


def _today():
    return time.strftime("%Y-%m-%d")


def _month():
    return time.strftime("%Y-%m")


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
    current_accounting = int(data.get("accounting_version", 1) or 1) >= 2
    _state = {
        "accounting_version": 2,
        "date": data.get("date", "") if current_accounting else "",
        "month": data.get("month", "") if current_accounting else "",
        "day_user": int(data.get("day_user", 0) or 0) if current_accounting else 0,
        "day_auto": int(data.get("day_auto", 0) or 0) if current_accounting else 0,
        "month_used": int(data.get("month_used", 0) or 0) if current_accounting else 0,
        "official_month_remaining": data.get("official_month_remaining"),
        "official_month_limit": data.get("official_month_limit"),
        "official_updated_at": float(data.get("official_updated_at", 0) or 0),
    }
    return _state


def _save_state():
    if _state is None:
        return
    try:
        atomic_write_json(_STATE_PATH, _state, indent=2)
    except Exception as error:
        log.warning("uapi state save failed: %s", error)


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
        state["official_month_remaining"] = None
        state["official_month_limit"] = None
        state["official_updated_at"] = 0
        changed = True
    return changed


def _limits(config):
    uapi_cfg = config.get("uapi", {}) if isinstance(config.get("uapi"), dict) else {}
    daily = int(uapi_cfg.get("daily_limit", 100) or 100)
    reserve = int(uapi_cfg.get("reserve", 30) or 30)
    month_limit = int(uapi_cfg.get("month_limit", 3400) or 3400)
    return daily, reserve, month_limit


def _endpoint_cost(path):
    return max(0, int(ENDPOINT_COSTS.get(path, 2)))


def credits_remaining(config):
    state = _load_state()
    if _rollover(state):
        _save_state()
    daily, reserve, month_limit = _limits(config)
    user_cap = max(0, daily - reserve)
    return {
        "user_left": max(0, user_cap - state["day_user"]),
        "user_cap": user_cap,
        "auto_left": max(0, reserve - state["day_auto"]),
        "auto_cap": reserve,
        "month_left": max(0, month_limit - state["month_used"]),
        "month_cap": month_limit,
        "month_used": state["month_used"],
        "day_used": state["day_user"] + state["day_auto"],
        "official_month_remaining": state.get("official_month_remaining"),
        "official_month_limit": state.get("official_month_limit"),
        "official_updated_at": state.get("official_updated_at", 0),
    }


def credits_available(config, kind="user", path=None):
    state = _load_state()
    if _rollover(state):
        _save_state()
    cost = _endpoint_cost(path) if path else 1
    if cost <= 0:
        return True
    daily, reserve, month_limit = _limits(config)
    if state["month_used"] + cost > month_limit:
        return False
    if kind == "auto":
        return (state["day_auto"] + cost <= reserve
                and state["day_user"] + state["day_auto"] + cost <= daily)
    return state["day_user"] + cost <= max(0, daily - reserve)


def _record_charge(config, path, kind, charged=None):
    amount = _endpoint_cost(path) if charged is None else max(0, int(charged))
    if amount <= 0:
        return True
    if not credits_available(config, kind, path=path) and charged is None:
        return False
    state = _load_state()
    if kind == "auto":
        state["day_auto"] += amount
    else:
        state["day_user"] += amount
    state["month_used"] += amount
    _save_state()
    return True


def _charge(config, path, kind):
    """Compatibility helper for tests and old callers."""
    if not credits_available(config, kind, path=path):
        return _endpoint_cost(path) <= 0
    return _record_charge(config, path, kind)


def _header_map(headers):
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _update_official_quota(headers):
    values = _header_map(headers)
    rate = values.get("ratelimit", "")
    policy = values.get("ratelimit-policy", "")
    remaining_match = re.search(r'billing-quota";r=(\d+)', rate)
    limit_match = re.search(r'billing-quota";q=(\d+)', policy)
    if not remaining_match and not limit_match:
        return
    state = _load_state()
    if remaining_match:
        state["official_month_remaining"] = int(remaining_match.group(1))
    if limit_match:
        state["official_month_limit"] = int(limit_match.group(1))
    state["official_updated_at"] = time.time()
    _save_state()


def _record_response(config, path, kind, status, headers):
    values = _header_map(headers)
    _update_official_quota(headers)
    charged_value = values.get("uapi-credits-charged")
    if charged_value is not None:
        try:
            charged = int(float(charged_value))
        except (TypeError, ValueError):
            charged = 0
    else:
        charged = _endpoint_cost(path) if int(status) == 200 else 0
    if charged > 0:
        _record_charge(config, path, kind, charged=charged)


def _cache_key(path, params):
    if not params:
        return (path, "")
    return (path, "&".join("{}={}".format(key, params[key]) for key in sorted(params)))


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
        for key, (timestamp, _) in list(_cache.items()):
            if timestamp < cutoff:
                _cache.pop(key, None)
    _cache[_cache_key(path, params)] = (time.time(), data)


def _api_key(config):
    return str(config.get("uapi_api_key") or "").strip()


def _auth_headers(config):
    key = _api_key(config)
    return {"Authorization": "Bearer " + key} if key else {}


def _request_lock(dispatcher):
    lock = getattr(dispatcher, "_uapi_request_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        dispatcher._uapi_request_lock = lock
    return lock


async def _json_request_unlocked(dispatcher, method, path, params=None, json_body=None,
                        kind="user", timeout=8, use_cache=False):
    if use_cache:
        cached = _cache_get(path, params)
        if cached is not None:
            return cached
    if not credits_available(dispatcher.config, kind, path=path):
        log.info("uapi: budget blocked %s kind=%s", path, kind)
        return None
    headers = _auth_headers(dispatcher.config)
    if not headers:
        _log_missing_key(path)
    attempts = [headers]
    if headers and _endpoint_cost(path) == 0:
        attempts.append({})
    session = dispatcher.client.session
    for index, attempt_headers in enumerate(attempts):
        try:
            async with session.request(
                method, BASE_URL + path, params=params, json=json_body,
                headers=attempt_headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                _record_response(dispatcher.config, path, kind,
                                 response.status, response.headers)
                if response.status == 401 and index + 1 < len(attempts):
                    log.warning("uapi %s rejected configured key; retrying free endpoint as visitor", path)
                    continue
                if response.status != 200:
                    log.warning("uapi %s -> HTTP %s", path, response.status)
                    return None
                data = await response.json(content_type=None)
                if use_cache:
                    _cache_put(path, params, data)
                return data
        except Exception as error:
            log.warning("uapi %s failed: %s", path, error)
            return None
    return None


async def _json_request(dispatcher, method, path, params=None, json_body=None,
                        kind="user", timeout=8, use_cache=False):
    async with _request_lock(dispatcher):
        return await _json_request_unlocked(
            dispatcher, method, path, params=params, json_body=json_body,
            kind=kind, timeout=timeout, use_cache=use_cache)


async def uapi_get(dispatcher, path, params=None, kind="user", timeout=8):
    return await _json_request(
        dispatcher, "GET", path, params=params, kind=kind,
        timeout=timeout, use_cache=True)


async def uapi_post(dispatcher, path, json_body=None, kind="user", timeout=8):
    return await _json_request(
        dispatcher, "POST", path, json_body=json_body or {}, kind=kind,
        timeout=timeout)


async def _uapi_get_binary_unlocked(dispatcher, path, params=None, kind="user",
                          max_bytes=6 * 1024 * 1024, timeout=20):
    if not credits_available(dispatcher.config, kind, path=path):
        log.info("uapi: budget blocked %s kind=%s", path, kind)
        return None
    headers = _auth_headers(dispatcher.config)
    if not headers:
        _log_missing_key(path)
    attempts = [headers]
    if headers and _endpoint_cost(path) == 0:
        attempts.append({})
    session = dispatcher.client.session
    for index, attempt_headers in enumerate(attempts):
        try:
            async with session.get(
                BASE_URL + path, params=params, headers=attempt_headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                _record_response(dispatcher.config, path, kind,
                                 response.status, response.headers)
                if response.status == 401 and index + 1 < len(attempts):
                    continue
                if response.status != 200:
                    log.warning("uapi %s -> HTTP %s", path, response.status)
                    return None
                content_type = response.headers.get("Content-Type", "image/jpeg")
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        log.warning("uapi %s exceeded %d bytes, abort", path, max_bytes)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks), content_type
        except Exception as error:
            log.warning("uapi %s failed: %s", path, error)
            return None
    return None


async def uapi_get_binary(dispatcher, path, params=None, kind="user",
                          max_bytes=6 * 1024 * 1024, timeout=20):
    async with _request_lock(dispatcher):
        return await _uapi_get_binary_unlocked(
            dispatcher, path, params=params, kind=kind,
            max_bytes=max_bytes, timeout=timeout)


async def _uapi_resolve_image_url_unlocked(dispatcher, path, params=None, timeout=8):
    if not credits_available(dispatcher.config, "user", path=path):
        log.info("uapi: budget blocked %s kind=user", path)
        return None
    headers = _auth_headers(dispatcher.config)
    if not headers:
        _log_missing_key(path)
    attempts = [headers]
    if headers and _endpoint_cost(path) == 0:
        attempts.append({})
    session = dispatcher.client.session
    for index, attempt_headers in enumerate(attempts):
        try:
            async with session.get(
                BASE_URL + path, params=params, headers=attempt_headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                _record_response(dispatcher.config, path, "user",
                                 response.status, response.headers)
                if response.status == 401 and index + 1 < len(attempts):
                    continue
                if response.status in (301, 302, 303, 307, 308):
                    return response.headers.get("Location")
                content_type = response.headers.get("Content-Type", "")
                if response.status == 200 and content_type.startswith("image/"):
                    log.info("uapi %s returned image directly (no redirect)", path)
                    return None
                log.warning("uapi %s -> HTTP %s", path, response.status)
                return None
        except Exception as error:
            log.warning("uapi %s failed: %s", path, error)
            return None
    return None


async def uapi_resolve_image_url(dispatcher, path, params=None, timeout=8):
    async with _request_lock(dispatcher):
        return await _uapi_resolve_image_url_unlocked(
            dispatcher, path, params=params, timeout=timeout)


async def refresh_official_quota(dispatcher):
    """Refresh official quota headers through a free endpoint that emits them."""
    await _json_request(
        dispatcher, "GET", "/saying", kind="user", timeout=8)
    return credits_remaining(dispatcher.config)


def reset_state_for_test():
    global _state
    _state = None
    _cache.clear()
    _missing_key_log_ts.clear()
