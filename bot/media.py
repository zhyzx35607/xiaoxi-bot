# bot/media.py - NapCat media helpers
import asyncio
import html
import logging
import re
import time

log = logging.getLogger("qqbot")


def _seg_data(seg):
    data = seg.get("data", {})
    return data if isinstance(data, dict) else {}


def _clean_text(text):
    text = html.unescape(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _flatten_message_text(message):
    if isinstance(message, str):
        return _clean_text(re.sub(r"\[CQ:[^\]]+\]", "", message))
    parts = []
    for seg in message or []:
        typ = seg.get("type")
        data = _seg_data(seg)
        if typ == "text":
            parts.append(data.get("text", ""))
        elif typ == "at":
            qq = data.get("qq", "")
            parts.append("@全体成员" if str(qq) == "all" else "@" + str(qq))
        elif typ == "image":
            summary = data.get("summary") or data.get("file") or "图片"
            parts.append("[图片:" + _clean_text(summary)[:40] + "]")
        elif typ == "record":
            parts.append("[语音]")
        elif typ == "file":
            parts.append("[文件:" + _clean_text(data.get("name") or data.get("file") or "")[:40] + "]")
        elif typ == "forward":
            parts.append("[合并转发]")
    return _clean_text(" ".join(parts))


def _runtime_int(dispatcher, key, default, minimum, maximum):
    try:
        value = int(dispatcher.config.get("runtime", {}).get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


async def resolve_image_reference(dispatcher, seg, timeout=None):
    """Resolve an image segment once, preferring its inline public URL."""
    data = _seg_data(seg)
    inline_url = str(data.get("url") or "").strip()
    if inline_url.startswith(("http://", "https://")):
        return inline_url
    file_id = str(data.get("file") or data.get("file_id") or "").strip()
    if not file_id:
        return ""
    if timeout is None:
        timeout = _runtime_int(dispatcher, "media_timeout_seconds", 12, 3, 30)
    try:
        result = await dispatcher.client.call(
            "get_image", {"file": file_id}, timeout=timeout)
    except Exception as error:
        log.debug("Image reference lookup failed: %s", error)
        return ""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return ""
    payload = result.get("data")
    payload = payload if isinstance(payload, dict) else {}
    resolved = str(payload.get("url") or payload.get("file") or "").strip()
    return resolved if resolved.startswith(("http://", "https://")) else ""


async def extract_message_context(dispatcher, group_id, message, raw_message="", max_items=5):
    """Build a concise media context for AI from NapCat/OneBot message segments."""
    if isinstance(message, str) or not message:
        return ""
    contexts = []
    for seg in message:
        if len(contexts) >= max_items:
            break
        typ = seg.get("type")
        try:
            if typ == "image":
                text = await describe_image_with_ocr(dispatcher, group_id, seg)
            elif typ == "forward":
                text = await describe_forward(dispatcher, seg)
            elif typ == "record":
                text = await describe_record(dispatcher, seg)
            elif typ == "file":
                text = describe_file_segment(seg)
            else:
                text = ""
            if text:
                contexts.append(text)
        except Exception as e:
            log.exception("Media context failed for %s: %s", typ, e)
    return "\n".join(contexts)


async def describe_image_with_ocr(dispatcher, group_id, seg):
    data = _seg_data(seg)
    file_id = data.get("file") or data.get("file_id") or ""
    sub_type = data.get("sub_type", "")
    summary = _clean_text(data.get("summary", ""))
    timeout = _runtime_int(dispatcher, "media_timeout_seconds", 12, 3, 30)
    deadline = time.monotonic() + timeout
    parts = []
    has_good_desc = False
    image_url = await resolve_image_reference(dispatcher, seg, timeout=timeout)
    cache_key = str(file_id or image_url)
    if cache_key:
        # Check dispatcher cache first (populated by _enhance_image_cache)
        cache = getattr(dispatcher, "_image_desc_cache", None)
        if cache and cache_key in cache:
            cached = cache[cache_key]
            cached_desc = cached if isinstance(cached, str) else cached.get("desc", "")
            if cached_desc:
                parts.append("图片：" + _clean_text(cached_desc)[:120])
                has_good_desc = True
        else:
            try:
                from .ai import describe_image
                remaining = max(0.1, deadline - time.monotonic())
                desc = await asyncio.wait_for(describe_image(
                    dispatcher, group_id, file_id, sub_type, summary,
                    image_url=image_url, lookup_timeout=remaining,
                    image_lookup_done=True, fallback_to_summary=False),
                    timeout=remaining)
                if desc and desc not in ("[图片]", "[表情/贴纸]"):
                    parts.append("图片：" + _clean_text(desc)[:120])
                    has_good_desc = True
                    if not hasattr(dispatcher, "_image_desc_cache"):
                        dispatcher._image_desc_cache = {}
                    dispatcher._image_desc_cache[cache_key] = {
                        "desc": _clean_text(desc)[:500],
                        "ts": time.time(),
                    }
            except asyncio.TimeoutError:
                log.debug("Image description timed out within media budget")
            except Exception:
                log.exception("Image description failed")

    # Stickers benefit from vision descriptions, but OCR is reserved for normal images.
    if not has_good_desc and str(sub_type or "0") == "0":
        image_ref = image_url or file_id
        if image_ref:
            max_attempts = _runtime_int(
                dispatcher, "image_ocr_max_attempts", 2, 0, 2)
            for api_name in ("ocr_image", "ocr_image_enhanced")[:max_attempts]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    result = await dispatcher.client.call(
                        api_name, {"image": image_ref}, timeout=remaining)
                    if result.get("status") != "ok":
                        continue
                    ocr_text = _extract_ocr_text(result.get("data"))
                    if ocr_text:
                        parts.append("OCR文字：" + ocr_text[:180])
                        break
                except Exception as error:
                    log.debug("OCR provider failed: api=%s error=%s", api_name, error)
                    continue
    if parts:
        return "；".join(dict.fromkeys(parts))
    if summary:
        return "图片：" + summary[:120]
    return "图片：[表情/贴纸]" if str(sub_type or "0") != "0" else "图片：[图片]"


def _extract_ocr_text(data):
    if not data:
        return ""
    texts = []
    if isinstance(data, dict):
        candidates = data.get("texts") or data.get("ocrResults") or data.get("words_result") or []
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, dict):
                    texts.append(item.get("text") or item.get("words") or item.get("content") or "")
                else:
                    texts.append(str(item))
        for key in ("text", "result", "words"):
            if data.get(key):
                texts.append(str(data.get(key)))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                texts.append(item.get("text") or item.get("words") or item.get("content") or "")
            else:
                texts.append(str(item))
    return _clean_text(" ".join(t for t in texts if t))


async def describe_forward(dispatcher, seg):
    data = _seg_data(seg)
    forward_id = data.get("id") or data.get("file") or data.get("resid")
    if not forward_id:
        return "合并转发：无法读取ID"
    result = await dispatcher.client.get_forward_msg(forward_id)
    if result.get("status") != "ok":
        return "合并转发：读取失败"
    nodes = result.get("data", {}).get("messages") or result.get("data", {}).get("news") or result.get("data", [])
    lines = []
    if isinstance(nodes, dict):
        nodes = nodes.get("messages", [])
    if not isinstance(nodes, list):
        return "合并转发：格式不认识"
    for item in nodes[:8]:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender", {})
        name = sender.get("nickname") or item.get("name") or "群友"
        content = item.get("message") or item.get("content") or item.get("message_chain") or ""
        text = _flatten_message_text(content)
        if text:
            lines.append(str(name)[:16] + ": " + text[:120])
    if not lines:
        return "合并转发：没有可读文字"
    return "合并转发内容：\n" + "\n".join(lines[:8])


async def describe_record(dispatcher, seg):
    data = _seg_data(seg)
    file_id = data.get("file") or data.get("file_id") or ""
    if not file_id:
        return "语音：收到一条语音"
    result = await dispatcher.client.get_record(file_id, "mp3")
    if result.get("status") == "ok":
        path = result.get("data", {}).get("file") or result.get("data", {}).get("url") or ""
        suffix = "，已转成mp3" if path else ""
        return "语音：收到一条语音" + suffix
    return "语音：收到一条语音，但暂时转码失败"


def describe_file_segment(seg):
    data = _seg_data(seg)
    name = _clean_text(data.get("name") or data.get("file") or data.get("file_id") or "未命名文件")
    size = data.get("size") or data.get("file_size")
    busid = data.get("busid")
    extra = []
    if size:
        try:
            size_i = int(size)
            if size_i >= 1024 * 1024:
                extra.append("{:.1f}MB".format(size_i / 1024 / 1024))
            elif size_i >= 1024:
                extra.append("{:.1f}KB".format(size_i / 1024))
            else:
                extra.append(str(size_i) + "B")
        except Exception:
            extra.append(str(size))
    if busid:
        extra.append("busid=" + str(busid))
    return "文件：" + name[:80] + (("（" + "，".join(extra) + "）") if extra else "")
