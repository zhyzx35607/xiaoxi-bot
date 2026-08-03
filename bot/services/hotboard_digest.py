"""Evidence collection and persona-aware hot-board digests."""
import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.parse
from html.parser import HTMLParser

import aiohttp

log = logging.getLogger("qqbot")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.description = ""
        self.paragraphs = []
        self._paragraph = False
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        attributes = {str(key).lower(): value for key, value in attrs}
        if tag == "p":
            self._paragraph = True
            self._buffer = []
        elif tag == "meta":
            name = str(attributes.get("name") or attributes.get("property") or "").lower()
            if name in {"description", "og:description", "twitter:description"}:
                value = str(attributes.get("content") or "").strip()
                if len(value) > len(self.description):
                    self.description = value

    def handle_endtag(self, tag):
        if tag == "p" and self._paragraph:
            value = _clean_text(" ".join(self._buffer))
            if len(value) >= 30:
                self.paragraphs.append(value[:500])
            self._paragraph = False
            self._buffer = []

    def handle_data(self, data):
        if self._paragraph:
            self._buffer.append(data)


async def _public_url(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            loop = asyncio.get_running_loop()
            records = await asyncio.wait_for(loop.getaddrinfo(
                host, parsed.port or 443, type=socket.SOCK_STREAM), timeout=3)
            addresses = [ipaddress.ip_address(record[4][0]) for record in records]
        return bool(addresses) and all(
            not address.is_private and not address.is_loopback
            and not address.is_link_local and not address.is_reserved
            and not address.is_multicast for address in addresses)
    except Exception:
        return False


async def _fetch_html(session, url, timeout=6, max_bytes=512 * 1024):
    current = str(url or "").strip()
    for _ in range(4):
        if not await _public_url(current):
            return "", current
        try:
            async with session.get(
                current, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
                timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        return "", current
                    current = urllib.parse.urljoin(current, location)
                    continue
                if response.status != 200:
                    return "", current
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(32768):
                    total += len(chunk)
                    if total > max_bytes:
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.charset or "utf-8", errors="replace"), current
        except Exception:
            return "", current
    return "", current


def _article_excerpt(document):
    if not document:
        return ""
    parser = _ArticleParser()
    try:
        parser.feed(document)
    except Exception:
        return ""
    values = [parser.description] if parser.description else []
    for paragraph in parser.paragraphs:
        if paragraph not in values:
            values.append(paragraph)
        if len(" ".join(values)) >= 900:
            break
    return " ".join(values)[:1000]


def _bing_results(document, limit=3):
    results = []
    for block in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', document, re.I | re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.I | re.S)
        if not link:
            continue
        snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.I | re.S)
        results.append({
            "title": _clean_text(link.group(2))[:160],
            "url": html.unescape(link.group(1)).strip(),
            "snippet": _clean_text(snippet.group(1) if snippet else "")[:500],
        })
        if len(results) >= limit:
            break
    return results


async def _collect_topic(session, item):
    title = _clean_text(item.get("title"))[:120]
    original_url = str(item.get("url") or "").strip()
    sources, evidence = [], []
    if original_url:
        document, final_url = await _fetch_html(session, original_url)
        excerpt = _article_excerpt(document)
        if excerpt:
            evidence.append(excerpt)
            sources.append(final_url)
    query_url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": title})
    document, _ = await _fetch_html(session, query_url, timeout=8)
    results = _bing_results(document)
    for result in results:
        if result["snippet"]:
            evidence.append(result["title"] + "?" + result["snippet"])
        if result["url"] and result["url"] not in sources:
            sources.append(result["url"])
    if results and len(" ".join(evidence)) < 500:
        document, final_url = await _fetch_html(session, results[0]["url"])
        excerpt = _article_excerpt(document)
        if excerpt:
            evidence.append(excerpt)
            if final_url not in sources:
                sources.insert(0, final_url)
    return {
        "title": title,
        "hot_value": str(item.get("hot_value") or "").strip(),
        "url": original_url,
        "evidence": "\n".join(evidence)[:1800],
        "sources": sources[:2],
    }


async def enrich_hotboard_items(dispatcher, items, limit=10):
    semaphore = asyncio.Semaphore(3)
    async def collect(item):
        async with semaphore:
            return await _collect_topic(dispatcher.client.session, item)
    results = await asyncio.gather(
        *(collect(item) for item in items[:limit]), return_exceptions=True)
    enriched = []
    for item, result in zip(items[:limit], results):
        if isinstance(result, Exception):
            result = {
                "title": _clean_text(item.get("title"))[:120],
                "hot_value": str(item.get("hot_value") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "evidence": "", "sources": [],
            }
        enriched.append(result)
    return enriched


def _fallback_digest(board_name, items):
    summary = "小汐扫了一圈，今天的{}热榜主要就是这些事。能核实到细节的写在每条里，没抓到可靠内容的不会瞎编。".format(board_name)
    details = [
        _clean_text(item.get("evidence"))[:220]
        or "暂时没抓到足够可靠的具体内容，先放标题和来源。"
        for item in items
    ]
    return summary, details


async def build_hotboard_digest(dispatcher, board, board_name, items, limit=10):
    cache = getattr(dispatcher, "_hotboard_digest_cache", None)
    if cache is None:
        cache = {}
        dispatcher._hotboard_digest_cache = cache
    cache_key = (str(board), tuple(str(item.get("title") or "") for item in items[:limit]))
    cached = cache.get(cache_key)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        enriched = await asyncio.wait_for(
            enrich_hotboard_items(dispatcher, items, limit=limit), timeout=40)
    except Exception as error:
        log.info("hotboard evidence collection failed: %s", error)
        enriched = [{"title": _clean_text(item.get("title"))[:120],
                     "hot_value": str(item.get("hot_value") or "").strip(),
                     "url": str(item.get("url") or "").strip(),
                     "evidence": "", "sources": []} for item in items[:limit]]
    overview, details = _fallback_digest(board_name, enriched)
    try:
        from ..ai import _call_deepseek
        evidence = [{"index": index, "title": item["title"],
                     "hot_value": item["hot_value"], "evidence": item["evidence"]}
                    for index, item in enumerate(enriched, 1)]
        messages = [{
            "role": "system",
            "content": (
                "你是QQ群机器人小汐。只根据证据解释今天具体发生了什么，不得根据标题脑补。"
                "语气自然直接，禁止‘作为AI’、‘综合来看’、‘引发广泛关注’等套话。"
                "严格输出JSON：{\"overview\":\"150到300字总览\",\"items\":[\"每条60到180字\"]}。"
                "items与输入顺序和数量一致；证据不足就明确说暂未确认。"),
        }, {"role": "user", "content": board_name + "热榜证据：\n" + json.dumps(evidence, ensure_ascii=False)}]
        text = await _call_deepseek(dispatcher.config, messages, max_tokens=1400,
                                    temperature=0.2, session=dispatcher.client.session)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.I | re.S)
        payload = json.loads(cleaned)
        candidate_overview = _clean_text(payload.get("overview"))[:600]
        candidate_details = payload.get("items")
        if not candidate_overview or not isinstance(candidate_details, list) or len(candidate_details) != len(enriched):
            raise ValueError("invalid digest shape")
        overview = candidate_overview
        details = [_clean_text(value)[:360] or details[index]
                   for index, value in enumerate(candidate_details)]
    except Exception as error:
        log.info("hotboard AI digest failed: %s", error)
    result = {"summary": overview, "details": details, "items": enriched}
    cache[cache_key] = (time.time(), result)
    if len(cache) > 20:
        cache.pop(min(cache, key=lambda key: cache[key][0]), None)
    return result
