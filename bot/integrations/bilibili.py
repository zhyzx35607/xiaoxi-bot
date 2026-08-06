"""bot/bilibili.py - Bilibili helpers: share parsing, mp4 download, UP主 push.

Operational notes (low-memory server):
  - Video info / play URL go through B站 official anonymous APIs (free).
    uapis.cn bilibili endpoints are only a fallback (they cost credits).
  - The archives poller uses the official wbi-signed endpoint every
    poll_interval seconds; on repeated risk-control failures it falls back
    to uapis.cn archives (auto credit bucket) at a slower cadence.
  - Video downloads stream to disk with a hard size cap; memory stays flat.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse

import aiohttp

from . import uapi
from ..storage.runtime_paths import create_runtime_temp_file
from ..utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PUSH_STATE_PATH = os.path.join(_ROOT, "data", "bili_push.json")

BV_RE = re.compile(r"BV1[0-9A-Za-z]{9}")
AV_RE = re.compile(r"av(\d{6,})", re.I)
B23_RE = re.compile(r"b23\.tv/([0-9A-Za-z]+)")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MIXIN_TABLE = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
               27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
               37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
               22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52)

# Module state: anonymous cookie + wbi keys + fallback bookkeeping.
_state = {
    "buvid3": "", "buvid4": "",
    "img_key": "", "sub_key": "", "wbi_ts": 0.0,
    "fail_count": 0, "fallback_until": 0.0, "last_uapi_poll": 0.0,
    "risk_until": 0.0, "last_risk_log": 0.0,
}
_push_state = None


# ---------- pure helpers (unit-testable) ----------

def extract_bvid(text):
    m = BV_RE.search(text or "")
    return m.group(0) if m else ""


def extract_av(text):
    m = AV_RE.search(text or "")
    return int(m.group(1)) if m else 0


def extract_b23(text):
    m = B23_RE.search(text or "")
    return "https://b23.tv/" + m.group(1) if m else ""


def mixin_key(img_key, sub_key):
    raw = (img_key + sub_key)
    return "".join(raw[i] for i in MIXIN_TABLE)[:32]


def wbi_sign(params, img_key, sub_key):
    """Sign params per B站 wbi rules. Returns a new dict with wts/w_rid."""
    key = mixin_key(img_key, sub_key)
    cleaned = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in params.items()
    }
    cleaned["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(cleaned.items()))
    cleaned["w_rid"] = hashlib.md5(
        (query + key).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return cleaned


def format_duration(seconds):
    seconds = int(seconds or 0)
    return "{}:{:02d}".format(*divmod(seconds, 60))


def format_count(num):
    num = float(num or 0)
    if num >= 10000:
        return "{:.1f}万".format(num / 10000)
    return str(int(num))


def format_video_text(v, link):
    stat = v.get("stat") or {}
    owner = v.get("owner") or {}
    desc = str(v.get("desc") or "").strip().replace("\n", " ")[:80]
    lines = [
        "【B站视频】{}".format(str(v.get("title") or "")[:60]),
        "UP主：{} · 时长 {}".format(owner.get("name", "?"),
                                  format_duration(v.get("duration", 0))),
        "播放 {} · 弹幕 {} · 点赞 {}".format(
            format_count(stat.get("view", 0)),
            format_count(stat.get("danmaku", 0)),
            format_count(stat.get("like", 0))),
    ]
    if desc and desc != "-":
        lines.append("简介：" + desc)
    lines.append(link)
    return "\n".join(lines)


# ---------- runtime state ----------

def _load_push_state():
    global _push_state
    if _push_state is not None:
        return _push_state
    try:
        with open(_PUSH_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _push_state = data
    return _push_state


def _save_push_state():
    if _push_state is None:
        return
    try:
        atomic_write_json(_PUSH_STATE_PATH, _push_state, indent=2)
    except Exception as e:
        log.warning("bili push state save failed: %s", e)


def _push_entry(group_id, mid):
    """Return the per-(group, mid) state dict, migrating the old list format."""
    state = _load_push_state()
    group = state.setdefault(str(group_id), {})
    entry = group.get(str(mid))
    if isinstance(entry, list):
        entry = {"seen": entry, "watermark": 0}
        group[str(mid)] = entry
    elif not isinstance(entry, dict):
        entry = {"seen": [], "watermark": 0}
        group[str(mid)] = entry
    entry.setdefault("seen", [])
    entry.setdefault("watermark", 0)
    return entry


def pushed_bvids(group_id, mid):
    return _push_entry(group_id, mid)["seen"]


def push_watermark(group_id, mid):
    return int(_push_entry(group_id, mid)["watermark"] or 0)


def mark_pushed(group_id, mid, bvids, watermark=None):
    entry = _push_entry(group_id, mid)
    seen = entry["seen"]
    for bvid in bvids:
        if bvid and bvid not in seen:
            seen.append(bvid)
    del seen[:-50]
    if watermark is not None:
        entry["watermark"] = max(int(entry["watermark"] or 0), int(watermark))
    _save_push_state()


def reset_state_for_test():
    global _push_state
    _push_state = None
    for key in ("buvid3", "buvid4", "img_key", "sub_key"):
        _state[key] = ""
    _state["wbi_ts"] = 0.0
    _state["fail_count"] = 0
    _state["fallback_until"] = 0.0
    _state["last_uapi_poll"] = 0.0
    _state["risk_until"] = 0.0
    _state["last_risk_log"] = 0.0


def _history_messages(result):
    """Return message records from common NapCat history response shapes."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return []
    data = result.get("data")
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("messages", "message_list", "list"):
        messages = data.get(key)
        if isinstance(messages, list):
            return messages
    return []


def _history_record_text(record):
    if not isinstance(record, dict):
        return ""
    parts = [record.get("raw_message"), record.get("message")]
    try:
        parts.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        pass
    return "\n".join(str(part) for part in parts if part is not None)


async def _recent_bot_message_contains(dispatcher, group_id, marker):
    """Best-effort confirmation for sends whose API response was uncertain."""
    if not marker:
        return False
    try:
        result = await dispatcher.client.get_group_msg_history(int(group_id), count=30)
    except Exception as e:
        log.info("bili delivery history check failed g=%s marker=%s: %s",
                 group_id, marker, e)
        return False
    bot_qq = int(dispatcher.config.get("bot_qq", 0) or 0)
    for record in _history_messages(result):
        sender = record.get("sender") if isinstance(record, dict) else None
        sender_id = 0
        if isinstance(sender, dict):
            sender_id = int(sender.get("user_id", 0) or 0)
        if not sender_id and isinstance(record, dict):
            sender_id = int(record.get("user_id", 0) or 0)
        if bot_qq and sender_id and sender_id != bot_qq:
            continue
        if marker in _history_record_text(record):
            return True
    return False


async def _send_group_confirmed(dispatcher, group_id, segments, marker, kind):
    """Send once and verify ambiguous timeouts through recent group history."""
    if await _recent_bot_message_contains(dispatcher, group_id, marker):
        log.info("%s already present in group history: g=%s marker=%s",
                 kind, group_id, marker)
        return {"status": "ok", "confirmed_by": "history"}
    result = await dispatcher.client.send_group_msg(int(group_id), segments)
    if isinstance(result, dict) and result.get("status") == "ok":
        return result
    status = result.get("status") if isinstance(result, dict) else result
    error_kind = result.get("error_kind") if isinstance(result, dict) else ""
    if status == "timeout" or error_kind == "timeout":
        await asyncio.sleep(2)
        if await _recent_bot_message_contains(dispatcher, group_id, marker):
            log.info("%s timeout confirmed through history: g=%s marker=%s",
                     kind, group_id, marker)
            return {"status": "ok", "confirmed_by": "history_after_timeout"}
    raise RuntimeError("{} send not confirmed: {}".format(kind, str(result)[:240]))


# ---------- B站 anonymous session (buvid cookie + wbi keys) ----------

_sessdata_warned = False


def _sessdata(dispatcher=None):
    """Configured B站 login cookie (env BILI_SESSDATA). Using it makes the
    official endpoints behave like normal account browsing - risk control
    against anonymous datacenter traffic no longer applies."""
    global _sessdata_warned
    if dispatcher is None:
        return _state.get("sessdata", "")
    value = str(dispatcher.config.get("bili_sessdata") or "").strip()
    if value != _state.get("sessdata", ""):
        _state["sessdata"] = value
        _sessdata_warned = False
    return value


def _headers(referer="https://www.bilibili.com", dispatcher=None):
    headers = {"User-Agent": UA, "Referer": referer}
    cookies = []
    sessdata = _sessdata(dispatcher)
    if sessdata:
        cookies.append("SESSDATA=" + sessdata)
    if _state["buvid3"]:
        cookies.append("buvid3={}; buvid4={}".format(
            _state["buvid3"], _state["buvid4"]))
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


async def _ensure_session(dispatcher):
    """Fetch anonymous buvid cookie and wbi keys (cached for ~12h)."""
    if _state["img_key"] and time.time() - _state["wbi_ts"] < 12 * 3600:
        return True
    session = dispatcher.client.session
    try:
        if not _state["buvid3"]:
            async with session.get(
                    "https://api.bilibili.com/x/frontend/finger/spi",
                    headers=_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
            payload = data.get("data") or {}
            _state["buvid3"] = payload.get("b_3", "")
            _state["buvid4"] = payload.get("b_4", "")
        global _sessdata_warned
        async with session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=_headers(dispatcher=dispatcher),
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if _sessdata(dispatcher) and data.get("code") == -101 and not _sessdata_warned:
            _sessdata_warned = True
            log.warning("bili: configured SESSDATA is invalid or expired, "
                        "falling back to anonymous access")
        wbi = ((data.get("data") or {}).get("wbi_img") or {})
        img_url = wbi.get("img_url", "")
        sub_url = wbi.get("sub_url", "")
        if not img_url or not sub_url:
            log.warning("bili: nav returned no wbi keys")
            return False
        _state["img_key"] = os.path.splitext(os.path.basename(img_url))[0]
        _state["sub_key"] = os.path.splitext(os.path.basename(sub_url))[0]
        _state["wbi_ts"] = time.time()
        return True
    except Exception as e:
        log.warning("bili session init failed: %s", e)
        return False


async def _bili_get(dispatcher, url, params=None, referer="https://www.bilibili.com",
                    timeout=10):
    session = dispatcher.client.session
    async with session.get(url, params=params,
                           headers=_headers(referer, dispatcher=dispatcher),
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            return {"code": -resp.status, "message": "http_{}".format(resp.status)}
        return await resp.json(content_type=None)


def _official_failed():
    """Count consecutive failed rounds; periodically refresh the anon session.

    B站 risk control is partly tied to the anonymous buvid cookie, so a
    fresh cookie + wbi key pair sometimes gets through again.
    """
    _state["fail_count"] += 1
    if _state["fail_count"] >= 3:
        _state["fail_count"] = 0
        _state["buvid3"] = ""
        _state["buvid4"] = ""
        _state["img_key"] = ""
        _state["sub_key"] = ""
        _state["wbi_ts"] = 0.0
        log.info("bili: refreshing anon session after repeated failures")


def _official_ok():
    _state["fail_count"] = 0


# ---------- video info / play url / download ----------

async def resolve_b23(dispatcher, short_url):
    """Resolve a b23.tv short link to its target URL (contains BV/av)."""
    try:
        session = dispatcher.client.session
        async with session.get(short_url, headers=_headers(),
                               allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                return resp.headers.get("Location", "")
    except Exception as e:
        log.warning("b23 resolve failed %s: %s", short_url, e)
    return ""


async def extract_video_ref(dispatcher, text):
    """Return ("bvid", str) / ("aid", int) found in text, resolving b23 links."""
    bvid = extract_bvid(text)
    if bvid:
        return "bvid", bvid
    aid = extract_av(text)
    if aid:
        return "aid", aid
    short = extract_b23(text)
    if short:
        target = await resolve_b23(dispatcher, short)
        bvid = extract_bvid(target)
        if bvid:
            return "bvid", bvid
        aid = extract_av(target)
        if aid:
            return "aid", aid
    return "", None


async def get_video_info(dispatcher, bvid="", aid=0):
    """Video info via official view API; uapis.cn videoinfo as fallback."""
    params = {"bvid": bvid} if bvid else {"aid": aid}
    try:
        data = await _bili_get(dispatcher,
                               "https://api.bilibili.com/x/web-interface/view",
                               params=params)
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            _official_ok()
            return data["data"]
        log.warning("bili view failed: code=%s", data.get("code"))
        _official_failed()
    except Exception as e:
        log.warning("bili view error: %s", e)
        _official_failed()
    # Fallback: uapis.cn (costs credits, user bucket)
    params = {"bvid": bvid} if bvid else {"aid": aid}
    data = await uapi.uapi_get(dispatcher, "/social/bilibili/videoinfo",
                               params=params, kind="user")
    if isinstance(data, dict) and (data.get("bvid") or data.get("aid")):
        return data
    return None


async def get_playurl_mp4(dispatcher, bvid, cid):
    """Anonymous mp4 play URL (platform=html5). Returns (url, size) or ("", 0)."""
    try:
        data = await _bili_get(
            dispatcher, "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "qn": 16,
                    "platform": "html5", "high_quality": 1},
            referer="https://www.bilibili.com/video/" + bvid)
        durl = ((data.get("data") or {}).get("durl") or [])
        if data.get("code") == 0 and durl:
            _official_ok()
            return durl[0].get("url", ""), int(durl[0].get("size", 0) or 0)
        log.warning("bili playurl failed: code=%s", data.get("code"))
    except Exception as e:
        log.warning("bili playurl error: %s", e)
    return "", 0


async def download_mp4(dispatcher, url, bvid, max_bytes, timeout=120):
    """Stream-download to tmp dir with hard size cap. Returns path or None."""
    path = ""
    referer = "https://www.bilibili.com/video/" + bvid
    try:
        session = dispatcher.client.session
        async with session.get(url, headers=_headers(referer),
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                log.warning("bili download HTTP %s", resp.status)
                return None
            length = int(resp.headers.get("Content-Length", 0) or 0)
            if length and length > max_bytes:
                log.info("bili download too large: %d > %d", length, max_bytes)
                return None
            total = 0
            fd, path = create_runtime_temp_file("bili_{}_".format(bvid), ".mp4")
            with os.fdopen(fd, "wb") as f:
                async for chunk in resp.content.iter_chunked(262144):
                    total += len(chunk)
                    if total > max_bytes:
                        f.close()
                        _remove_quiet(path)
                        log.info("bili download aborted at %d bytes", total)
                        return None
                    f.write(chunk)
        return path
    except Exception as e:
        log.warning("bili download error: %s", e)
        _remove_quiet(path)
        return None


def _remove_quiet(path):
    try:
        os.remove(path)
    except Exception:
        pass


# ---------- archives (UP主 new-video polling) ----------

async def get_archives(dispatcher, mid, count=5):
    """Latest uploads of a UP主. Free official API first, every round;
    uapis.cn only as a rate-limited, budget-capped fallback.

    Returns a list of {"bvid","title","cover","created","duration","mid","author"}.
    """
    now = time.time()
    bili_cfg = dispatcher.config.get("bilibili", {})
    risk_until = float(_state.get("risk_until", 0.0) or 0.0)
    risk_code = None
    if now >= risk_until and await _ensure_session(dispatcher):
        retries = max(1, min(4, int(bili_cfg.get("official_retries", 2) or 2)))
        for attempt in range(retries):
            try:
                params = wbi_sign({
                    "mid": mid, "pn": 1, "ps": count, "order": "pubdate",
                    "platform": "web", "web_location": 1550101,
                }, _state["img_key"], _state["sub_key"])
                data = await _bili_get(
                    dispatcher,
                    "https://api.bilibili.com/x/space/wbi/arc/search",
                    params=params,
                    referer="https://space.bilibili.com/{}/video".format(mid))
                vlist = (((data.get("data") or {}).get("list") or {}).get("vlist") or [])
                code = data.get("code")
                if code == 0 and isinstance(vlist, list):
                    _official_ok()
                    _state["risk_until"] = 0.0
                    return [{
                        "bvid": v.get("bvid", ""),
                        "title": v.get("title", ""),
                        "cover": v.get("pic", ""),
                        "created": int(v.get("created", 0) or 0),
                        "duration": v.get("length", ""),
                        "mid": mid,
                        "author": v.get("author", ""),
                    } for v in vlist]
                if code in (-352, -412):
                    risk_code = code
                    break
                log.info("bili arc/search attempt %d failed: code=%s", attempt + 1, code)
            except Exception as e:
                log.info("bili arc/search attempt %d error: %s", attempt + 1, e)
            if attempt + 1 < retries:
                await asyncio.sleep(2 + attempt)
        if risk_code is not None:
            cooldown = max(300, int(bili_cfg.get("risk_cooldown_seconds", 1800) or 1800))
            _state["risk_until"] = now + cooldown
            if now - float(_state.get("last_risk_log", 0.0) or 0.0) >= 300:
                _state["last_risk_log"] = now
                log.warning("bili arc/search risk-controlled code=%s; paused for %ss",
                            risk_code, cooldown)
        else:
            log.warning("bili arc/search failed after retries")
        _official_failed()
    # uapis.cn fallback: optional, max one call per 5min, auto credit bucket
    if not bili_cfg.get("uapi_fallback", True):
        return []
    now = time.time()
    if now - _state["last_uapi_poll"] < 300:
        return []
    _state["last_uapi_poll"] = now
    data = await uapi.uapi_get(dispatcher, "/social/bilibili/archives",
                               params={"mid": mid, "pn": 1, "ps": count},
                               kind="auto")
    videos = (data or {}).get("videos") if isinstance(data, dict) else None
    if not videos:
        return []
    return [{
        "bvid": v.get("bvid", ""),
        "title": v.get("title", ""),
        "cover": v.get("cover", ""),
        "created": int(v.get("publish_time", v.get("create_time", 0)) or 0),
        "duration": v.get("duration", ""),
        "mid": mid,
        "author": "",
    } for v in videos]


# ---------- dynamics (follow-feed based, requires SESSDATA) ----------

async def get_dynamics_feed(dispatcher):
    """Fetch the logged-in account's dynamics timeline (feed/all).

    Requires BILI_SESSDATA. One request covers every watched UP主 that the
    account follows. Returns raw items list or [].
    """
    if not _sessdata(dispatcher):
        return []
    if not await _ensure_session(dispatcher):
        return []
    try:
        params = wbi_sign({
            "timezone_offset": "-480", "type": "all", "platform": "web",
            "features": "itemOpusStyle", "web_location": "333.999", "page": "1",
        }, _state["img_key"], _state["sub_key"])
        data = await _bili_get(
            dispatcher,
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
            params=params, referer="https://t.bilibili.com")
        if data.get("code") == 0:
            _official_ok()
            items = (data.get("data") or {}).get("items") or []
            return items if isinstance(items, list) else []
        log.info("bili feed/all failed: code=%s", data.get("code"))
        _official_failed()
    except Exception as e:
        log.warning("bili feed/all error type=%s detail=%r", type(e).__name__, e)
        _official_failed()
    return []


def parse_dynamic_item(item):
    """Normalize one feed item into {id,mid,name,ts,text,images,link}.

    Returns None for video-upload dynamics (handled by archives polling)
    and for items that carry no readable content.
    """
    if not isinstance(item, dict):
        return None
    dyn_id = str(item.get("id_str") or "")
    if not dyn_id:
        return None
    item_type = str(item.get("type") or "")
    if item_type == "DYNAMIC_TYPE_AV":
        return None  # video uploads are pushed by the archives poller
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    mid = int(author.get("mid", 0) or 0)
    name = str(author.get("name") or "")
    ts = int(author.get("pub_ts", 0) or 0)
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}
    text = ""
    images = []
    prefix = ""
    target = item
    if item_type == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if isinstance(orig, dict):
            parsed_orig = parse_dynamic_item(orig)
            if parsed_orig:
                text = parsed_orig["text"]
                images = parsed_orig["images"]
                prefix = "转发了 @" + (parsed_orig["name"] or "原主") + "："
    else:
        opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
        summary = opus.get("summary") or {}
        text = str(summary.get("text") or "")
        pics = opus.get("pics") or []
        if isinstance(pics, list):
            images = [str(p.get("url")) for p in pics
                      if isinstance(p, dict) and p.get("url")]
        draw = major.get("draw") if isinstance(major.get("draw"), dict) else {}
        for d_item in (draw.get("items") or []):
            if isinstance(d_item, dict) and d_item.get("src"):
                images.append(str(d_item["src"]))
        if not text:
            desc = major.get("desc") if isinstance(major.get("desc"), dict) else {}
            text = str(desc.get("text") or "")
    text = prefix + text.strip()
    if not text and not images:
        return None
    return {
        "id": dyn_id, "mid": mid, "name": name, "ts": ts,
        "text": text, "images": images[:3],
        "link": "https://t.bilibili.com/" + dyn_id,
    }


def parse_av_dynamic(item):
    """Extract upload info from a DYNAMIC_TYPE_AV feed item -> video dict."""
    if not isinstance(item, dict):
        return None
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}
    archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
    bvid = str(archive.get("bvid") or "")
    if not bvid:
        return None
    return {
        "bvid": bvid,
        "title": str(archive.get("title") or ""),
        "author": str(author.get("name") or ""),
        "cover": str(archive.get("cover") or ""),
        "created": int(author.get("pub_ts", 0) or 0),
    }


def _dyn_entry(group_id, mid):
    entry = _push_entry(group_id, mid)
    entry.setdefault("dyn_seen", [])
    entry.setdefault("dyn_watermark", 0)
    return entry


async def _announce_dynamic(dispatcher, group_id, dyn):
    from ..permission import get_bot_role
    text = "【B站动态】{}\n{}\n{}".format(
        dyn["name"], dyn["text"][:300], dyn["link"])
    segments = []
    bot_role, _ = await get_bot_role(dispatcher, int(group_id))
    if bot_role in ("admin", "owner"):
        segments.append({"type": "at", "data": {"qq": "all"}})
    segments.append({"type": "text", "data": {"text": text}})
    for url in dyn["images"]:
        if url.startswith("//"):
            url = "https:" + url
        segments.append({"type": "image", "data": {"file": url}})
    result = await _send_group_confirmed(
        dispatcher, group_id, segments, dyn["id"], "bili dynamic")
    log.info("bili dynamic push: group=%s id=%s status=%s",
             group_id, dyn["id"], result.get("status"))
    return result


async def poll_dynamics_once(dispatcher):
    """One dynamics polling round (single feed request for all watched mids).

    The follow feed carries both regular dynamics and video uploads, so with
    SESSDATA configured this replaces the risk-control-prone arc/search
    polling entirely. First-sight entries (no watermark yet) announce at
    most the single newest item and only if it is fresh (<=30 min old), so
    the bot never floods groups with historical dynamics.
    """
    watch = _watched_mids(dispatcher)
    if not watch or not _sessdata(dispatcher):
        return 0
    try:
        items = await get_dynamics_feed(dispatcher)
    except Exception as e:
        log.warning("bili dynamics poll failed: %s", e)
        return 0
    announced = 0
    state_changed = False
    now = int(time.time())
    dyn_candidates = {}
    av_candidates = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        author = ((item.get("modules") or {}).get("module_author")) or {}
        try:
            mid = int(author.get("mid", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mid not in watch:
            continue
        if str(item.get("type") or "") == "DYNAMIC_TYPE_AV":
            video = parse_av_dynamic(item)
            if video:
                av_candidates.setdefault(mid, []).append(video)
            continue
        dyn = parse_dynamic_item(item)
        if dyn:
            dyn_candidates.setdefault(mid, []).append(dyn)
    # --- regular dynamics (text / picture / forward) ---
    for mid, dyns in dyn_candidates.items():
        dyns.sort(key=lambda item: int(item.get("ts", 0) or 0), reverse=True)
        max_ts = max([d["ts"] for d in dyns] or [0])
        for gid in watch[mid]:
            entry = _dyn_entry(gid, mid)
            watermark = int(entry["dyn_watermark"] or 0)
            virgin = not watermark and not entry["dyn_seen"]
            fresh = []
            for d in dyns:
                if d["id"] in entry["dyn_seen"]:
                    continue
                if watermark and (not d["ts"] or d["ts"] <= watermark):
                    continue
                fresh.append(d)
            if virgin:
                fresh = [d for d in fresh
                         if d["ts"] and d["ts"] >= now - 1800][:1]
            confirmed_max = 0
            for d in reversed(fresh):
                try:
                    await _announce_dynamic(dispatcher, gid, d)
                    announced += 1
                    entry["dyn_seen"].append(d["id"])
                    confirmed_max = max(confirmed_max, int(d["ts"] or 0))
                    state_changed = True
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning("bili dynamic announce failed g=%s: %s", gid, e)
                    break
            del entry["dyn_seen"][:-50]
            if confirmed_max:
                entry["dyn_watermark"] = max(watermark, confirmed_max)
            elif virgin and not fresh and max_ts:
                entry["dyn_watermark"] = max_ts
                state_changed = True
    # --- video uploads carried by the feed ---
    for mid, videos in av_candidates.items():
        videos.sort(key=lambda item: int(item.get("created", 0) or 0), reverse=True)
        max_ts = max([v["created"] for v in videos] or [0])
        for gid in watch[mid]:
            seen = pushed_bvids(gid, mid)
            watermark = push_watermark(gid, mid)
            virgin = not watermark and not seen
            new_videos = []
            for v in videos:
                if v["bvid"] in seen:
                    continue
                if watermark and (not v["created"] or v["created"] <= watermark):
                    continue
                new_videos.append(v)
            if virgin:
                new_videos = [v for v in new_videos
                              if v["created"] and v["created"] >= now - 1800][:1]
            announced_bvids = []
            announced_max = 0
            for video in reversed(new_videos):
                try:
                    await _announce_video(dispatcher, gid, video)
                    announced += 1
                    announced_bvids.append(video["bvid"])
                    announced_max = max(announced_max, video["created"])
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning("bili announce failed g=%s: %s", gid, e)
                    break
            if announced_bvids:
                mark_pushed(gid, mid, announced_bvids,
                            watermark=announced_max or None)
            elif virgin and not new_videos and max_ts:
                mark_pushed(gid, mid, [], watermark=max_ts)
    if announced or state_changed:
        _save_push_state()
    return announced


# ---------- push loop ----------

def _watched_mids(dispatcher):
    """Collect {mid: [group_id, ...]} from per-group bili_push config."""
    watch = {}
    groups = dispatcher.config.get("groups", {})
    for gid, gcfg in groups.items():
        if not isinstance(gcfg, dict) or gcfg.get("enabled") is not True:
            continue
        push_cfg = gcfg.get("bili_push") or {}
        for mid in push_cfg.get("mids", []) or []:
            try:
                mid = int(mid)
            except (TypeError, ValueError):
                continue
            watch.setdefault(mid, []).append(str(gid))
    return watch


async def _announce_video(dispatcher, group_id, video):
    from ..permission import get_bot_role
    bvid = video.get("bvid", "")
    title = video.get("title", "")
    author = video.get("author", "")
    link = "https://www.bilibili.com/video/" + bvid
    head = "【B站新投稿】"
    if author:
        head += " {}".format(author)
    text = "{}\n{}\n{}".format(head, title[:60], link)
    segments = []
    bot_role, _ = await get_bot_role(dispatcher, int(group_id))
    if bot_role in ("admin", "owner"):
        segments.append({"type": "at", "data": {"qq": "all"}})
    segments.append({"type": "text", "data": {"text": text}})
    cover = video.get("cover", "")
    if cover:
        if cover.startswith("//"):
            cover = "https:" + cover
        segments.append({"type": "image", "data": {"file": cover}})
    result = await _send_group_confirmed(
        dispatcher, group_id, segments, bvid, "bili video")
    log.info("bili push: group=%s bvid=%s status=%s",
             group_id, bvid, result.get("status"))
    return result


async def poll_once(dispatcher):
    """One polling round; returns count of newly announced videos."""
    watch = _watched_mids(dispatcher)
    announced = 0
    for mid, group_ids in watch.items():
        try:
            videos = await get_archives(dispatcher, mid, count=5)
        except Exception as e:
            log.warning("bili poll mid=%s failed: %s", mid, e)
            continue
        if not videos:
            continue
        for gid in group_ids:
            seen = pushed_bvids(gid, mid)
            watermark = push_watermark(gid, mid)
            # Only genuinely-new uploads: unseen AND newer than the watermark.
            # (The uapis.cn fallback can return stale/older lists; the watermark
            # prevents those from ever being announced.)
            new_videos = []
            for v in videos:
                bvid = v.get("bvid")
                if not bvid or bvid in seen:
                    continue
                created = int(v.get("created", 0) or 0)
                if watermark and (not created or created <= watermark):
                    continue
                new_videos.append(v)
            announced_bvids = []
            announced_max = 0
            # Oldest first so timeline reads naturally and failures stop watermarks.
            new_videos.sort(key=lambda item: int(item.get("created", 0) or 0))
            for video in new_videos:
                try:
                    await _announce_video(dispatcher, gid, video)
                    announced += 1
                    announced_bvids.append(video["bvid"])
                    announced_max = max(announced_max,
                                        int(video.get("created", 0) or 0))
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning("bili announce failed g=%s: %s", gid, e)
                    break
            if announced_bvids:
                mark_pushed(gid, mid, announced_bvids,
                            watermark=announced_max or None)
        await asyncio.sleep(1)
    return announced


async def push_loop(dispatcher):
    interval = 60
    log.info("Bilibili push loop started")
    try:
        while dispatcher.client._running:
            cfg = dispatcher.config.get("bilibili", {})
            interval = max(30, int(cfg.get("poll_interval", 60) or 60))
            if not getattr(dispatcher.client, "is_connected", True):
                await asyncio.sleep(min(5, interval))
                continue
            try:
                if _watched_mids(dispatcher):
                    if _sessdata(dispatcher):
                        # The follow feed carries dynamics and uploads in one
                        # request; arc/search gets IP risk-controlled (-412)
                        # from cloud hosts, so prefer the feed.
                        await poll_dynamics_once(dispatcher)
                    else:
                        await poll_once(dispatcher)
            except Exception as e:
                log.warning("bili poll round failed: %s", e)
            # Sleep in small chunks to stay responsive to shutdown
            for _ in range(interval):
                if not dispatcher.client._running:
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    log.info("Bilibili push loop stopped")


async def prime_push_state(dispatcher, group_id, mid):
    """Initialize seen-list + watermark when a mid is first added.

    If the fetch fails we still set the watermark to now, so historical
    uploads are never announced.
    """
    videos = []
    try:
        videos = await get_archives(dispatcher, mid, count=5)
    except Exception as e:
        log.warning("bili push prime failed mid=%s: %s", mid, e)
    bvids = [v["bvid"] for v in videos if v.get("bvid")]
    watermark = max([int(v.get("created", 0) or 0) for v in videos] or [0])
    if not watermark:
        watermark = int(time.time())
    mark_pushed(group_id, mid, bvids, watermark=watermark)
    # Prime dynamics watermark the same way (best-effort)
    try:
        items = await get_dynamics_feed(dispatcher)
        dyn_watermark = 0
        for item in items:
            dyn = parse_dynamic_item(item)
            if dyn and dyn["mid"] == int(mid):
                entry = _dyn_entry(group_id, mid)
                if dyn["id"] not in entry["dyn_seen"]:
                    entry["dyn_seen"].append(dyn["id"])
                dyn_watermark = max(dyn_watermark, dyn["ts"] or 0)
        entry = _dyn_entry(group_id, mid)
        entry["dyn_watermark"] = dyn_watermark or int(time.time())
        _save_push_state()
    except Exception as e:
        log.warning("bili dynamics prime failed mid=%s: %s", mid, e)
        entry = _dyn_entry(group_id, mid)
        if not entry["dyn_watermark"]:
            entry["dyn_watermark"] = int(time.time())
            _save_push_state()
    return videos


# ---------- group share handling (auto parse + download) ----------

async def handle_share(dispatcher, group_id, text):
    """Detect a B站 video link in a group message, reply info + video file."""
    kind, ref = await extract_video_ref(dispatcher, text)
    if not kind:
        return False
    bili_cfg = dispatcher.config.get("bilibili", {})
    max_bytes = int(bili_cfg.get("download_max_mb", 80) or 80) * 1024 * 1024
    info = await get_video_info(dispatcher,
                                bvid=ref if kind == "bvid" else "",
                                aid=ref if kind == "aid" else 0)
    if not info:
        return False
    bvid = info.get("bvid") or (ref if kind == "bvid" else "")
    if not bvid:
        return False
    link = "https://www.bilibili.com/video/" + bvid
    text_reply = format_video_text(info, link)
    cover = info.get("pic", "")
    segments = [{"type": "text", "data": {"text": text_reply}}]
    if cover:
        segments.append({"type": "image", "data": {"file": cover}})
    await dispatcher.client.send_group_msg(group_id, segments)

    cid = info.get("cid")
    if not cid:
        return True
    url, size = await get_playurl_mp4(dispatcher, bvid, cid)
    if not url:
        return True
    if size and size > max_bytes:
        log.info("bili %s too large (%d bytes), info-only", bvid, size)
        return True
    dispatcher.create_background_task(
        _download_and_send(dispatcher, group_id, url, bvid, max_bytes),
        name="bili_download",
    )
    return True


async def _download_and_send(dispatcher, group_id, url, bvid, max_bytes):
    path = await download_mp4(dispatcher, url, bvid, max_bytes)
    if not path:
        return
    try:
        result = await dispatcher.client.send_group_msg(
            group_id, [{"type": "video", "data": {"file": "file://" + path}}])
        log.info("bili video sent: group=%s bvid=%s status=%s",
                 group_id, bvid, result.get("status"))
    except Exception as e:
        log.warning("bili video send failed: %s", e)
    finally:
        _remove_quiet(path)
