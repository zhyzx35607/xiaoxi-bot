"""Web search providers and result formatting."""

import logging
import re
import time
import urllib.parse

import aiohttp

log = logging.getLogger("qqbot")
_SEARCH_CACHE_TTL = 600

async def search_web(dispatcher, query):
    """Search web: uapis.cn aggregate search first, Bing HTML scrape as fallback."""
    config = dispatcher.config
    ws_cfg = config.get("web_search", {})
    if not ws_cfg.get("enabled", True):
        return ""
    
    import re as _re_ws
    query = _re_ws.sub(r"\s+", " ", (query or "")).strip()
    if len(query) < 4:
        return ""
    cache_key = query.lower()[:120]
    cache = getattr(dispatcher, "_web_search_cache", None)
    now = time.time()
    if cache is not None:
        cached = cache.get(cache_key)
        if cached:
            age = now - cached.get("ts", 0)
            hit_value = cached.get("value", "")
            # Successful results: full TTL. Empty/failed results: short TTL (120s).
            effective_ttl = _SEARCH_CACHE_TTL if hit_value else 120
            if age < effective_ttl:
                return hit_value
    
    try:
        async with dispatcher._search_sem:
            value = await _search_web_uapi(dispatcher, query)
            if not value:
                value = await _search_web_bing(dispatcher, query)
            if cache is not None:
                cache[cache_key] = {"ts": now, "value": value}
                if len(cache) > 100:
                    # Lazy cleanup: remove entries older than 30 min (bulk of stale cache)
                    stale = [k for k, v in cache.items() if now - v.get("ts", 0) > 1800]
                    for key in stale[:50]:
                        cache.pop(key, None)
                    # If still over limit, do a full sort once
                    if len(cache) > 100:
                        oldest = sorted(cache.items(), key=lambda item: item[1].get("ts", 0))[:20]
                        for key, _ in oldest:
                            cache.pop(key, None)
            return value
    except Exception as e:
        log.error("Web search error: %s", e)
    
    return ""

async def _search_web_uapi(dispatcher, query):
    """Primary search path: uapis.cn aggregate search via the credit channel."""
    from bot import uapi
    if not uapi.credits_available(
            dispatcher.config, "user", path="/search/aggregate"):
        log.debug("uapi search skipped: credit budget exhausted")
        return ""
    data = await uapi.uapi_post(dispatcher, "/search/aggregate",
                                json_body={"query": str(query)[:80]}, kind="user")
    if not data:
        return ""
    value = _format_uapi_search_results(data)
    if value:
        log.info("Web search via uapi completed: result_chars=%d", len(value))
    return value

def _format_uapi_search_results(data, limit=3):
    """Best-effort normalize the uapis aggregate search payload to text lines."""
    items = []
    if isinstance(data, dict):
        for key in ("results", "data", "list", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            inner = data.get("data")
            if isinstance(inner, dict):
                for key in ("results", "list", "items"):
                    if isinstance(inner.get(key), list):
                        items = inner[key]
                        break
    elif isinstance(data, list):
        items = data
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()[:100]
        snippet = str(item.get("snippet") or item.get("content")
                      or item.get("description") or item.get("desc") or "").strip()[:150]
        url = str(item.get("url") or item.get("link") or "").strip()
        if not title and not snippet:
            continue
        line = title or snippet[:100]
        if title and snippet:
            line += "\n  " + snippet
        if url:
            line += "\n  " + url
        lines.append(line)
        if len(lines) >= limit:
            break
    return "\n".join(lines)

async def _search_web_bing(dispatcher, query):
    """Fallback search path: Bing HTML scrape (fragile, mainland-friendly)."""
    encoded = urllib.parse.quote(query)
    url = f"https://www.bing.com/search?q={encoded}&setlang=zh-cn"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=6)
    if dispatcher.client.session:
        session = dispatcher.client.session
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                html = await resp.text()
                value = _parse_bing_results(html, query)
                if value:
                    log.info("Web search completed through Bing fallback")
                return value
    else:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    value = _parse_bing_results(html, query)
                    if value:
                        log.info("Web search completed through Bing fallback")
                    return value
    return ""

def _parse_bing_results(html, query):
    """Parse Bing HTML search results with multi-layer fallback."""
    import re as _re_b
    results = []
    # Layer 1: standard b_algo blocks
    blocks = _re_b.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)
    for block in blocks[:3]:
        title_m = _re_b.search(r'<h2[^>]*><a[^>]*>(.*?)</a>', block, re.DOTALL)
        snippet_m = _re_b.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        if title_m:
            title = _re_b.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            title = title.replace("&ensp;", " ").replace("&emsp;", " ")
            snippet = ""
            if snippet_m:
                snippet = _re_b.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()
                snippet = snippet.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                snippet = snippet.replace("&ensp;", " ").replace("&emsp;", " ")
                # Remove date prefixes like "2025年12月15日"
                snippet = _re_b.sub(r'^\d{4}年\d{1,2}月\d{1,2}日\s*', '', snippet)
            line = title[:100]
            if snippet:
                line += "\n  " + snippet[:150]
            results.append(line)
    # Layer 2: fallback to b_caption / generic result snippets
    if not results:
        alt_blocks = _re_b.findall(r'<li class="b_caption"[^>]*>(.*?)</li>', html, re.DOTALL)
        if not alt_blocks:
            alt_blocks = _re_b.findall(r'<div class="b_caption"[^>]*>(.*?)</div>', html, re.DOTALL)
        for block in alt_blocks[:3]:
            text = _re_b.sub(r'<[^>]+>', ' ', block)
            text = _re_b.sub(r'\s+', ' ', text).strip()
            if len(text) > 20:
                results.append(text[:250])
    # Layer 3: extract page title as confirmation search worked
    if not results:
        title_m = _re_b.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE)
        if title_m:
            title = title_m.group(1).strip()
            if "No results" not in title and "没有结果" not in title:
                results.append("搜索已完成，但未能解析详情")
    if not results:
        return ""
    return "\n".join(results[:3])
