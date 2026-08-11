"""Bounded client for the Mukyu random image service."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from urllib.parse import urljoin, urlparse

import aiohttp


log = logging.getLogger("qqbot")

_DEFAULT_BASE_URL = "https://i.mukyu.ru"
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_JSON_BYTES = 256 * 1024
_MAX_COOLDOWN_SECONDS = 3600.0
_failure_streak = 0
_cooldown_until = 0.0


class MukyuError(RuntimeError):
    """Raised when the image service returns an unusable response."""


@dataclass(frozen=True)
class MukyuImage:
    url: str
    image_id: int
    x_restrict: int
    width: int
    height: int
    extension: str
    ai_type: int | None
    illust_type: int | None


def _settings(config):
    value = config.get("mukyu_images", {})
    return value if isinstance(value, dict) else {}


def _base_url(config):
    configured = str(_settings(config).get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    parsed = urlparse(configured)
    if parsed.scheme != "https" or not parsed.hostname:
        return _DEFAULT_BASE_URL
    return configured


def _headers(config):
    headers = {
        "Accept": "application/json",
        "User-Agent": "XiaoxiQQBot/1.0 (+https://github.com/zhyzx35607/xiaoxi-bot)",
    }
    api_key = str(config.get("mukyu_api_key") or "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _clamp_int(value, minimum, maximum, default=None):
    if value is None or value == "":
        return default
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _retry_after(response):
    try:
        return max(30.0, min(_MAX_COOLDOWN_SECONDS, float(
            response.headers.get("Retry-After", "") or 60)))
    except (TypeError, ValueError):
        return 60.0


def _start_cooldown(seconds=None):
    global _cooldown_until, _failure_streak
    _failure_streak += 1
    delay = float(seconds or min(_MAX_COOLDOWN_SECONDS, 30 * (2 ** min(_failure_streak, 7))))
    _cooldown_until = max(_cooldown_until, time.monotonic() + delay)
    return delay


def _clear_cooldown():
    global _cooldown_until, _failure_streak
    _cooldown_until = 0.0
    _failure_streak = 0


async def _read_json_bounded(response, max_bytes):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise MukyuError("image metadata response is too large")
        except ValueError:
            pass
    payload = await response.content.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise MukyuError("image metadata response is too large")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MukyuError("image metadata response is invalid JSON") from error
    if not isinstance(data, dict):
        raise MukyuError("image metadata response is not an object")
    return data


def _validated_image(data, base_url, requested_r18):
    if data.get("ok") is not True:
        raise MukyuError(str(data.get("message") or data.get("code") or "image service rejected request"))
    payload = data.get("data")
    image = payload.get("image") if isinstance(payload, dict) else None
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(image, dict) or not isinstance(urls, dict):
        raise MukyuError("image metadata is incomplete")

    x_restrict = _clamp_int(image.get("x_restrict"), 0, 2)
    if x_restrict is None:
        raise MukyuError("image rating is missing")
    if requested_r18 == 0 and x_restrict != 0:
        raise MukyuError("image service returned restricted content for a safe request")

    local_path = str(urls.get("local") or "").strip()
    if not local_path.startswith("/i/") or "//" in local_path:
        raise MukyuError("image service returned an unsafe local path")
    image_url = urljoin(base_url + "/", local_path.lstrip("/"))
    base_host = urlparse(base_url).hostname
    parsed_url = urlparse(image_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != base_host:
        raise MukyuError("image URL escaped the configured service origin")

    return MukyuImage(
        url=image_url,
        image_id=_clamp_int(image.get("id"), 1, 2**63 - 1, 0) or 0,
        x_restrict=x_restrict,
        width=_clamp_int(image.get("width"), 1, 100000, 0) or 0,
        height=_clamp_int(image.get("height"), 1, 100000, 0) or 0,
        extension=str(image.get("ext") or "").lower()[:8],
        ai_type=_clamp_int(image.get("ai_type"), 0, 2),
        illust_type=_clamp_int(image.get("illust_type"), 0, 2),
    )


async def fetch_random_image(
        dispatcher, *, r18=0, tags=None, tag_mode="or", orientation=None,
        min_pixels=None, min_bookmarks=None, ai_type=None, illust_type=None):
    """Fetch one validated random image metadata record."""
    settings = _settings(dispatcher.config)
    if not settings.get("enabled", True):
        raise MukyuError("image service is disabled")
    remaining = _cooldown_until - time.monotonic()
    if remaining > 0:
        raise MukyuError("image service is cooling down")

    requested_r18 = _clamp_int(r18, 0, 2, 0)
    params = {"format": "simple_json", "strategy": "random", "r18": requested_r18}
    clean_tags = [str(tag).strip()[:64] for tag in (tags or []) if str(tag).strip()][:20]
    if clean_tags:
        params["tags"] = ",".join(clean_tags)
        params["tag_mode"] = "and" if str(tag_mode).lower() == "and" else "or"
    if orientation in {"landscape", "portrait", "square"}:
        params["orientation"] = orientation
    for key, value, maximum in (
            ("min_pixels", min_pixels, 100_000_000),
            ("min_bookmarks", min_bookmarks, 10_000_000)):
        normalized = _clamp_int(value, 0, maximum)
        if normalized is not None:
            params[key] = normalized
    normalized_ai = _clamp_int(ai_type, 0, 2)
    if normalized_ai is not None:
        params["ai_type"] = normalized_ai
    normalized_type = _clamp_int(illust_type, 0, 2)
    if normalized_type is not None:
        params["illust_type"] = normalized_type

    base_url = _base_url(dispatcher.config)
    timeout = max(3.0, min(60.0, float(settings.get("timeout_seconds", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)))
    max_bytes = _clamp_int(
        settings.get("max_json_bytes"), 16 * 1024, 1024 * 1024,
        _DEFAULT_MAX_JSON_BYTES,
    )
    try:
        async with dispatcher.client.session.get(
                base_url + "/random", params=params, headers=_headers(dispatcher.config),
                timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as response:
            if response.status == 429:
                delay = _start_cooldown(_retry_after(response))
                log.warning("Mukyu image API rate limited; cooling down for %.0fs", delay)
                raise MukyuError("image service is rate limited")
            if response.status != 200:
                delay = _start_cooldown()
                log.warning("Mukyu image API returned HTTP %s; cooldown %.0fs", response.status, delay)
                raise MukyuError("image service returned HTTP {}".format(response.status))
            data = await _read_json_bounded(response, max_bytes)
    except MukyuError:
        raise
    except Exception as error:
        delay = _start_cooldown()
        log.warning("Mukyu image API request failed; cooldown %.0fs: %s", delay, error)
        raise MukyuError("image service request failed") from error

    result = _validated_image(data, base_url, requested_r18)
    _clear_cooldown()
    return result


def reset_state_for_test():
    _clear_cooldown()
