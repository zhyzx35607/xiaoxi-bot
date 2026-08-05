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
from datetime import datetime
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


_CATEGORY_KEYWORDS = (
    ("教育考试", ("教育", "学校", "考试", "笔试", "面试", "高考", "成绩", "招生")),
    ("社会法治", ("法院", "警方", "案件", "死刑", "死缓", "出狱", "调查", "回应")),
    ("经济民生", ("经济", "GDP", "机票", "燃油", "价格", "就业", "消费", "航线")),
    ("天气灾害", ("台风", "暴雨", "高温", "地震", "天气", "洪水")),
    ("文娱体育", ("电视剧", "电影", "演员", "综艺", "樊振东", "体育", "奥运", "比赛")),
    ("出行生活", ("抢票", "12306", "火车票", "冬瓜", "睡觉", "降温", "旅行")),
)
_DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_RELATIVE_TIME_RE = r"\d+\s*(?:分钟|小时|天|周|个月)(?:前|之前)"


def _normalized_key(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _clean_text(value).lower())


def _text_overlap(left, right):
    left_chars, right_chars = set(_normalized_key(left)), set(_normalized_key(right))
    if not left_chars or not right_chars:
        return 0.0
    return len(left_chars & right_chars) / max(1, min(len(left_chars), len(right_chars)))


def _search_result_is_stale(result, now=None, max_age_days=120):
    current = now or datetime.now()
    dates = []
    for year, month, day in _DATE_RE.findall(
            "{} {}".format(result.get("title", ""), result.get("snippet", ""))):
        try:
            dates.append(datetime(int(year), int(month), int(day)))
        except ValueError:
            continue
    past_dates = [value for value in dates if value <= current]
    return bool(past_dates) and all(
        (current - value).days > max_age_days for value in past_dates)


def _prepare_evidence_fragment(title, value):
    text = _clean_text(value).replace("�", "")
    text = re.sub(
        rf"[?？](?=(?:{_RELATIVE_TIME_RE}|20\d{{2}}年))", " · ", text)
    if "：" in text:
        prefix, remainder = text.split("：", 1)
        if remainder.strip() and len(prefix) <= 180 and _text_overlap(prefix, title) >= 0.45:
            text = remainder.strip()
    normalized_title = _normalized_key(title)
    while normalized_title and _normalized_key(text).startswith(normalized_title):
        text = text[len(_clean_text(title)):].lstrip(" -—·•|丨:：?？")
        if not text:
            break
    text = re.sub(
        rf"^(?:\s*[·•|丨-]\s*)*(?:{_RELATIVE_TIME_RE}|20\d{{2}}年\d{{1,2}}月\d{{1,2}}日)\s*[·•|丨-]?\s*",
        "", text)
    text = re.sub(
        rf"\s*[·•|丨]\s*{_RELATIVE_TIME_RE}\s*(?:[·•|丨-]\s*)?",
        " ", text)
    return _clean_text(text)


def _summarize_evidence(title, evidence, max_chars=170):
    selected, seen = [], []
    for block in re.split(r"[\r\n]+", str(evidence or "")):
        prepared = _prepare_evidence_fragment(title, block)
        if not prepared:
            continue
        sentences = re.split(r"(?<=[。！？!?；;])\s*", prepared)
        for sentence in sentences:
            sentence = sentence.strip(" -—|丨")
            key = _normalized_key(sentence)
            if len(key) < 10 or key == _normalized_key(title):
                continue
            if any(key in previous or previous in key for previous in seen):
                continue
            remaining = max_chars - len("".join(selected))
            if remaining <= 12:
                break
            if len(sentence) > remaining:
                sentence = sentence[:max(12, remaining - 1)].rstrip("，、；;：:") + "…"
                key = _normalized_key(sentence)
            selected.append(sentence)
            seen.append(key)
            if len(selected) >= 2 or len("".join(selected)) >= max_chars:
                break
        if len(selected) >= 2 or len("".join(selected)) >= max_chars:
            break
    return "".join(selected) or "暂未查到足够可靠的具体细节。"


def _fallback_overview(board_name, items):
    counts = {name: 0 for name, _ in _CATEGORY_KEYWORDS}
    for item in items:
        title = _clean_text(item.get("title"))
        for category, keywords in _CATEGORY_KEYWORDS:
            if any(keyword.lower() in title.lower() for keyword in keywords):
                counts[category] += 1
                break
    ranked = [name for name, count in sorted(
        counts.items(), key=lambda pair: pair[1], reverse=True) if count]
    titles = ["「{}」".format(_clean_text(item.get("title"))[:36])
              for item in items[:3] if _clean_text(item.get("title"))]
    verified = sum(bool(_clean_text(item.get("evidence"))) for item in items)
    focus = "、".join(ranked[:3]) if ranked else "社会热点"
    overview = "今天的{}热榜主要集中在{}。".format(board_name, focus)
    if titles:
        overview += "热度靠前的包括{}。".format("、".join(titles))
    if verified == len(items):
        overview += "每条都找到了可供提炼的公开信息。"
    elif verified:
        overview += "已从公开来源提炼{}条要点，其余条目会明确标注证据不足。".format(verified)
    else:
        overview += "目前没有抓到足够可靠的细节，只保留标题和来源。"
    return overview


def _extract_json_object(value):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(value or "").strip(), flags=re.I | re.S)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("digest payload is not an object")
    return payload


def _merge_ai_details(payload, fallback_details):
    values = payload.get("items")
    if not isinstance(values, list):
        return list(fallback_details), 0
    mapped = {}
    for position, entry in enumerate(values):
        index, value = position, entry
        if isinstance(entry, dict):
            try:
                index = int(entry.get("index", position + 1)) - 1
            except (TypeError, ValueError):
                index = position
            value = (entry.get("summary") or entry.get("detail")
                     or entry.get("text") or entry.get("content"))
        clean = _clean_text(value)[:220]
        if 0 <= index < len(fallback_details) and clean and not re.search(r"https?://", clean):
            mapped[index] = clean
    return [mapped.get(index, fallback) for index, fallback in enumerate(fallback_details)], len(mapped)


async def _collect_topic(session, item):
    title = _clean_text(item.get("title"))[:120]
    original_url = str(item.get("url") or "").strip()
    sources, evidence, evidence_keys = [], [], set()

    def add_evidence(value):
        clean = _clean_text(value)
        key = _normalized_key(clean)
        if len(key) >= 10 and key not in evidence_keys:
            evidence.append(clean)
            evidence_keys.add(key)

    if original_url:
        document, final_url = await _fetch_html(session, original_url)
        excerpt = _article_excerpt(document)
        if excerpt:
            add_evidence(excerpt)
            sources.append(final_url)
    now = datetime.now()
    query = "{} {}年{}月".format(title, now.year, now.month)
    query_url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
    document, _ = await _fetch_html(session, query_url, timeout=8)
    results = [result for result in _bing_results(document)
               if not _search_result_is_stale(result, now=now)]
    for result in results:
        if result["snippet"]:
            add_evidence(result["snippet"])
        if result["url"] and result["url"] not in sources:
            sources.append(result["url"])
    if results and len(" ".join(evidence)) < 500:
        document, final_url = await _fetch_html(session, results[0]["url"])
        excerpt = _article_excerpt(document)
        if excerpt:
            add_evidence(excerpt)
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
    summary = _fallback_overview(board_name, items)
    details = [_summarize_evidence(item.get("title"), item.get("evidence"))
               for item in items]
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
        evidence = [{
            "index": index,
            "title": item["title"],
            "hot_value": item["hot_value"],
            "evidence": item["evidence"][:1200],
            "fallback": details[index - 1],
        } for index, item in enumerate(enriched, 1)]
        messages = [{
            "role": "system",
            "content": (
                "你是QQ群机器人小汐。只根据证据概括今天具体发生了什么，不得根据标题脑补。"
                "删除搜索结果标题、时间标签和重复句，不要复制网页摘要，不要输出网址。"
                "语气自然直接，禁止‘作为AI’、‘综合来看’、‘引发广泛关注’等套话。"
                "严格输出JSON：{\"overview\":\"80到160字总览\",\"items\":[\"每条35到90字的一句话概括\"]}。"
                "items与输入顺序一致；证据不足就写‘暂未确认更多细节’。"),
        }, {"role": "user", "content": board_name + "热榜证据：\n" + json.dumps(evidence, ensure_ascii=False)}]
        text = await _call_deepseek(dispatcher.config, messages, max_tokens=1800,
                                    temperature=0.15, session=dispatcher.client.session)
        payload = _extract_json_object(text)
        candidate_overview = _clean_text(payload.get("overview"))[:360]
        candidate_details, valid_count = _merge_ai_details(payload, details)
        if candidate_overview:
            overview = candidate_overview
        if valid_count:
            details = candidate_details
        if not candidate_overview and not valid_count:
            raise ValueError("AI digest contained no usable content")
        if valid_count != len(enriched):
            log.info("hotboard AI digest partial: items=%d/%d overview=%s",
                     valid_count, len(enriched), bool(candidate_overview))
    except Exception as error:
        log.info("hotboard AI digest failed: %s", error)
    result = {"summary": overview, "details": details, "items": enriched}
    cache[cache_key] = (time.time(), result)
    if len(cache) > 20:
        cache.pop(min(cache, key=lambda key: cache[key][0]), None)
    return result
