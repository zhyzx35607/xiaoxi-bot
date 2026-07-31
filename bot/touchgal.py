"""TouchGal metadata search and safe resource-page replies."""

import difflib
import logging
import re
import time
import unicodedata
from urllib.parse import urlparse

import aiohttp


log = logging.getLogger("qqbot")

DEFAULT_API_BASE = "https://developer.touchgal.com/api"
DEFAULT_SITE_BASE = "https://www.touchgal.ink"
_ALLOWED_SITE_HOSTS = {"www.touchgal.ink", "touchgal.ink"}
_CACHE = {}
_CACHE_MAX = 200

_REQUEST_TERMS = (
    "资源", "下载", "网盘", "度盘", "直装", "怎么下", "哪里下",
    "怎么下载", "求一个", "求一份", "求一下", "求", "有没有", "有无", "谁有",
)
_STRONG_REQUEST_TERMS = (
    "资源", "下载", "网盘", "度盘", "直装", "怎么下", "哪里下", "怎么下载", "谁有",
)
_NOISE_PATTERNS = (
    r"^(?:请问|问下|想问一下|麻烦|帮忙|帮我|可以|能不能|有没有|有无|谁有)\s*",
    r"^(?:求一个|求一份|求一下|求个|求)\s*",
    r"\s*(?:怎么下|哪里下|怎么下载|下载地址|下载链接|度盘链接|网盘链接)\s*$",
    r"\s*(?:资源|下载|度盘|网盘|链接|地址)\s*$",
    r"\s*(?:有吗|有没有|可以吗|吗|么|呢|一下)\s*$",
)
_PLATFORM_TERMS = {
    "android": ("安卓直装", "安卓", "android", "手机端", "手机版"),
    "krkr": ("krkr", "kr2", "吉里吉里"),
    "windows": ("windows", "win版", "pc版", "电脑端", "电脑版", "pc"),
    "pe": ("pe版", "pe"),
}
_PLATFORM_LABELS = {
    "android": "Android直装",
    "krkr": "KRKR",
    "windows": "Windows/PC",
    "pe": "移动端/PE",
}


def _bounded_number(value, default, minimum, maximum, cast):
    try:
        number = cast(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _settings(dispatcher):
    cfg = dispatcher.config.get("touchgal", {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "auto_reply": bool(cfg.get("auto_reply", True)),
        "allow_nsfw": bool(cfg.get("allow_nsfw", False)),
        "timeout": _bounded_number(cfg.get("timeout_seconds"), 8, 3, 20, float),
        "cache_ttl": _bounded_number(cfg.get("cache_ttl_seconds"), 600, 60, 86400, int),
        "auto_min_score": _bounded_number(cfg.get("auto_min_score"), 84, 60, 100, int),
        "auto_cooldown": _bounded_number(cfg.get("auto_cooldown_seconds"), 20, 5, 300, int),
        "max_results": _bounded_number(cfg.get("max_results"), 5, 1, 10, int),
        "max_resources": _bounded_number(cfg.get("max_resources"), 3, 1, 5, int),
        "token": str(dispatcher.config.get("touchgal_api_token") or "").strip(),
        "api_base": _safe_base_url(
            dispatcher.config.get("touchgal_api_base_url") or DEFAULT_API_BASE,
            DEFAULT_API_BASE,
        ),
        "site_base": _safe_base_url(
            cfg.get("site_base_url") or DEFAULT_SITE_BASE,
            DEFAULT_SITE_BASE,
            allowed_hosts=_ALLOWED_SITE_HOSTS,
        ),
    }


def _safe_base_url(value, fallback, allowed_hosts=None):
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return fallback
    if parsed.scheme != "https" or not parsed.hostname:
        return fallback
    if allowed_hosts is not None and parsed.hostname.casefold() not in allowed_hosts:
        return fallback
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return fallback
    return str(value).strip().rstrip("/")


def normalize_title(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("＊", "*").replace("・", "")
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", text)


def _detect_platform(text):
    lowered = str(text or "").casefold()
    for name, terms in _PLATFORM_TERMS.items():
        if any(term.casefold() in lowered for term in terms):
            return name
    return ""


def _strip_platform_terms(text):
    value = str(text or "")
    for terms in _PLATFORM_TERMS.values():
        for term in sorted(terms, key=len, reverse=True):
            value = re.sub(re.escape(term), " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def parse_command_query(raw):
    text = re.sub(r"\[CQ:[^\]]+\]", "", str(raw or "")).strip()
    platform = _detect_platform(text)
    title = _strip_platform_terms(text).strip(" \t\r\n，。！？!?：:、;；-—_~～")
    return {"title": title, "platform": platform}


def extract_resource_request(raw):
    text = re.sub(r"\[CQ:[^\]]+\]", "", str(raw or "")).strip()
    if not text or not any(term.casefold() in text.casefold() for term in _REQUEST_TERMS):
        return None
    bracket = re.search(r"《(.+?)》|「(.+?)」|『(.+?)』|【(.+?)】", text)
    quoted = re.search(r"[\"“](.+?)[\"”]|['‘](.+?)['’]", text)
    captured = next((value for value in (bracket.groups() if bracket else ()) if value), "")
    if not captured and quoted:
        captured = next((value for value in quoted.groups() if value), "")
    title = captured.strip() if captured else text
    if not captured:
        for pattern in _NOISE_PATTERNS:
            title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()
        title = _strip_platform_terms(title)
    title = title.strip(" \t\r\n，。！？!?：:、;；-—_~～")
    if not title or len(title) > 80:
        return None
    platform = _detect_platform(text)
    lowered = text.casefold()
    strong = bool(captured or any(term.casefold() in lowered for term in _STRONG_REQUEST_TERMS))
    if not strong and not platform and not captured and re.search(
        r"(?:有没有人|有无谁|一起|打游戏|玩游戏|今晚|明晚|聊天)", text,
    ):
        return None
    return {"title": title, "platform": platform, "strong": strong, "raw": text}


def _cache_get(key, ttl):
    item = _CACHE.get(key)
    if item and time.monotonic() - item[0] < ttl:
        return item[1]
    if item:
        _CACHE.pop(key, None)
    return None


def _cache_put(key, value):
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.monotonic(), value)


async def _api_get(dispatcher, path, params=None):
    settings = _settings(dispatcher)
    if not settings["token"]:
        return {"ok": False, "error": "not_configured"}
    headers = {"Authorization": "Bearer " + settings["token"]}
    url = settings["api_base"] + path
    try:
        async with dispatcher.client.session.get(
            url, params=params or {}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=settings["timeout"]),
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = {}
            if response.status == 200:
                if not isinstance(payload, dict):
                    return {"ok": True, "data": payload}
                if payload.get("success") is not False:
                    return {"ok": True, "data": payload.get("data") or {}}
            payload_error = payload.get("error") if isinstance(payload, dict) else None
            error = payload_error.get("code") if isinstance(payload_error, dict) else payload_error
            if response.status == 401:
                return {"ok": False, "error": "unauthorized"}
            if response.status == 429:
                return {"ok": False, "error": "rate_limited"}
            if response.status == 403:
                return {"ok": False, "error": "forbidden"}
            if response.status == 404:
                return {"ok": False, "error": "not_found"}
            log.warning("TouchGal API %s -> HTTP %s error=%s", path, response.status, error)
            return {"ok": False, "error": "server_error" if response.status >= 500 else "api_error"}
    except Exception as error:
        log.warning("TouchGal API %s failed: %s", path, error)
        return {"ok": False, "error": "network_error"}


def _extract_items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "results", "list", "games", "resources"):
        if isinstance(data.get(key), list):
            return data[key]
    nested = data.get("data")
    return _extract_items(nested) if nested is not data else []


def _first_text(item, *keys):
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _text_list(value):
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，/|]", value) if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _safe_site_link(value, site_base):
    link = str(value or "").strip()
    if not link:
        return ""
    try:
        target = urlparse(link)
        allowed = urlparse(site_base)
    except ValueError:
        return ""
    if target.scheme not in ("http", "https"):
        return ""
    if target.hostname != allowed.hostname:
        return ""
    return link


async def search_games(dispatcher, keyword, limit=5):
    settings = _settings(dispatcher)
    normalized = normalize_title(keyword)
    if len(normalized) < 2:
        return {"ok": False, "error": "query_too_short"}
    cache_key = ("search", normalized, settings["allow_nsfw"], int(limit))
    cached = _cache_get(cache_key, settings["cache_ttl"])
    if cached is not None:
        return cached
    result = await _api_get(dispatcher, "/v1/games/search", {
        "keyword": str(keyword).strip(),
        "page": 1,
        "limit": max(1, min(int(limit), 10)),
        "allowNsfw": str(settings["allow_nsfw"]).lower(),
    })
    if not result.get("ok"):
        return result
    items = _extract_items(result.get("data"))
    games = []
    for item in items:
        if not isinstance(item, dict):
            continue
        unique_id = _first_text(item, "uniqueId", "unique_id", "id")
        name = _first_text(item, "name", "title", "gameName")
        if re.fullmatch(r"[A-Za-z0-9]{8}", unique_id) and name:
            aliases = _text_list(item.get("aliases") or item.get("alias") or [])
            for key in ("originalName", "original_name", "chineseName", "japaneseName"):
                alias = _first_text(item, key)
                if alias and alias not in aliases and alias != name:
                    aliases.append(alias)
            games.append({"unique_id": unique_id, "name": name, "aliases": aliases})
    response = {"ok": True, "items": games}
    _cache_put(cache_key, response)
    return response


async def get_resources(dispatcher, unique_id):
    settings = _settings(dispatcher)
    if not re.fullmatch(r"[A-Za-z0-9]{8}", str(unique_id or "")):
        return {"ok": False, "error": "invalid_id"}
    cache_key = ("resources", unique_id, settings["allow_nsfw"])
    cached = _cache_get(cache_key, settings["cache_ttl"])
    if cached is not None:
        return cached
    result = await _api_get(dispatcher, "/v1/games/{}/resources".format(unique_id), {
        "allowNsfw": str(settings["allow_nsfw"]).lower(),
    })
    if not result.get("ok"):
        return result
    items = _extract_items(result.get("data"))
    resources = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not settings["allow_nsfw"] and bool(item.get("isNsfw") or item.get("nsfw")):
            continue
        deep_link = _safe_site_link(
            _first_text(item, "deepLink", "deep_link", "detailUrl", "detail_url"),
            settings["site_base"],
        )
        resources.append({
            "name": _first_text(item, "name", "title", "resourceName") or "资源",
            "description": _first_text(item, "description", "summary"),
            "categories": _text_list(item.get("categories") or item.get("category")),
            "sizes": _text_list(item.get("sizes") or item.get("size")),
            "deep_link": deep_link,
        })
    response = {"ok": True, "items": resources}
    _cache_put(cache_key, response)
    return response


def _candidate_score(query, candidate_name):
    query_norm = normalize_title(query)
    name_norm = normalize_title(candidate_name)
    if not query_norm or not name_norm:
        return 0
    if query_norm == name_norm:
        return 100
    if len(query_norm) >= 3 and query_norm in name_norm:
        coverage = len(query_norm) / max(len(name_norm), 1)
        return min(94, int(82 + coverage * 12))
    if len(name_norm) >= 3 and name_norm in query_norm:
        return 84
    ratio = difflib.SequenceMatcher(None, query_norm, name_norm).ratio()
    return int(ratio * 80)


def rank_candidates(query, items):
    ranked = []
    for item in items or []:
        names = [item.get("name", "")] + list(item.get("aliases") or [])
        score = max((_candidate_score(query, name) for name in names), default=0)
        ranked.append({**item, "score": score})
    return sorted(ranked, key=lambda item: item["score"], reverse=True)


def select_candidate(query, items, min_score=84):
    ranked = rank_candidates(query, items)
    if not ranked:
        return None, []
    top = ranked[0]
    second_score = ranked[1]["score"] if len(ranked) > 1 else 0
    if top["score"] >= min_score and top["score"] - second_score >= 8:
        return top, ranked
    if top["score"] == 100:
        return top, ranked
    return None, ranked


def _platform_labels(resource):
    text = " ".join([
        resource.get("name", ""), resource.get("description", ""),
        " ".join(resource.get("categories") or []),
    ]).casefold()
    labels = []
    checks = (
        ("Android直装", ("安卓直装", "android")),
        ("KRKR", ("krkr", "kr2", "吉里吉里")),
        ("Windows/PC", ("windows", "pc版", "电脑端", "电脑版")),
        ("移动端/PE", ("pe版", "手机端", "手机版", " pe ")),
    )
    for label, terms in checks:
        if any(term in text for term in terms):
            labels.append(label)
    return labels


def _resource_matches_platform(resource, platform):
    if not platform:
        return False
    expected = _PLATFORM_LABELS.get(platform, "").casefold()
    labels = [label.casefold() for label in _platform_labels(resource)]
    if expected and expected in labels:
        return True
    haystack = " ".join([
        resource.get("name", ""), resource.get("description", ""),
        " ".join(resource.get("categories") or []),
    ]).casefold()
    return any(term.casefold() in haystack for term in _PLATFORM_TERMS.get(platform, ()))


def _resource_line(resource):
    labels = _platform_labels(resource)
    sizes = resource.get("sizes") or []
    suffix = []
    if labels:
        suffix.append("/".join(labels))
    if sizes:
        suffix.append("、".join(sizes[:2]))
    name = resource.get("name") or "游戏资源"
    return "{}{}".format(name, "（{}）".format("；".join(suffix)) if suffix else "")


def format_candidates(query, ranked, settings):
    lines = ["“{}”可能对应多部作品，请用 /gal 完整名称 再查：".format(query)]
    for index, item in enumerate(ranked[:3], 1):
        lines.append("{}. {}\n   {}/{}".format(
            index, item["name"], settings["site_base"], item["unique_id"]
        ))
    return "\n".join(lines)


async def search_and_format(dispatcher, query, platform="", explicit=False):
    settings = _settings(dispatcher)
    if not settings["enabled"]:
        return {"handled": explicit, "text": "TouchGal 查询功能当前未启用" if explicit else ""}
    if not settings["token"]:
        return {
            "handled": explicit,
            "text": "TouchGal API Token 还没有配置；拿到 Token 后设置 TOUCHGAL_API_TOKEN 即可启用查询"
            if explicit else "",
        }
    search = await search_games(dispatcher, query, settings["max_results"])
    if not search.get("ok"):
        messages = {
            "query_too_short": "作品名太短，可能对应多部游戏，请提供更完整的名称",
            "unauthorized": "TouchGal API Token 无效或尚未获批",
            "forbidden": "TouchGal API Token 没有该接口权限或尚未获批",
            "rate_limited": "TouchGal 查询额度暂时用完了，稍后再试",
            "network_error": "TouchGal 暂时连接不上，稍后再试",
            "server_error": "TouchGal 服务暂时异常，稍后再试",
        }
        text = messages.get(search.get("error"), "TouchGal 查询失败，稍后再试")
        return {"handled": explicit or search.get("error") == "query_too_short", "text": text}
    items = search.get("items") or []
    if not items:
        return {"handled": explicit, "text": "TouchGal 暂时没找到《{}》".format(query) if explicit else ""}
    selected, ranked = select_candidate(query, items, settings["auto_min_score"])
    if not selected:
        return {
            "handled": explicit,
            "text": format_candidates(query, ranked, settings) if explicit else "",
        }
    resources = await get_resources(dispatcher, selected["unique_id"])
    resource_items = resources.get("items") if resources.get("ok") else []
    if platform and resource_items:
        preferred = [item for item in resource_items if _resource_matches_platform(item, platform)]
        if preferred:
            resource_items = preferred + [item for item in resource_items if item not in preferred]
    detail_link = "{}/{}".format(settings["site_base"], selected["unique_id"])
    if resource_items:
        detail_link = resource_items[0].get("deep_link") or detail_link
    lines = ["找到《{}》".format(selected["name"])]
    if platform:
        lines.append("优先查找平台：{}".format(_PLATFORM_LABELS.get(platform, platform)))
    if resource_items:
        lines.append("可用资源：")
        lines.extend("- " + _resource_line(item) for item in resource_items[:settings["max_resources"]])
    elif resources.get("ok"):
        lines.append("官方 API 暂未返回公开资源分类")
    else:
        lines.append("资源分类暂时读取失败，可打开详情页查看")
    lines.append("TouchGal 详情/获取资源：{}".format(detail_link))
    lines.append("链接会打开站内资源区域，不是网盘直链")
    return {"handled": True, "text": "\n".join(lines), "selected": selected}


async def handle_auto_request(dispatcher, group_id, user_id, raw):
    settings = _settings(dispatcher)
    if not settings["enabled"] or not settings["auto_reply"] or not settings["token"]:
        return False
    request = extract_resource_request(raw)
    if not request:
        return False
    from event_policy import allow_event
    if not allow_event(
        "touchgal_auto", "{}:{}".format(group_id, user_id), settings["auto_cooldown"]
    ):
        return False
    result = await search_and_format(
        dispatcher, request["title"], request["platform"], explicit=request["strong"]
    )
    if not result.get("handled") or not result.get("text"):
        return False
    await dispatcher.client.send_group_msg(group_id, [
        {"type": "at", "data": {"qq": str(user_id)}},
        {"type": "text", "data": {"text": "\n" + result["text"]}},
    ])
    log.info("TouchGal auto reply group=%s user=%s query=%s selected=%s",
             group_id, user_id, request["title"],
             (result.get("selected") or {}).get("unique_id", ""))
    return True


def reset_cache_for_test():
    _CACHE.clear()
