# bot/media.py - NapCat media helpers
import html
import logging
import re

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
            log.error("Media context failed for %s: %s", typ, e, exc_info=True)
    return "\n".join(contexts)


async def describe_image_with_ocr(dispatcher, group_id, seg):
    data = _seg_data(seg)
    file_id = data.get("file") or data.get("file_id") or ""
    sub_type = data.get("sub_type", "")
    summary = _clean_text(data.get("summary", ""))
    parts = []
    has_good_desc = False
    if file_id:
        # Check dispatcher cache first (populated by _enhance_image_cache)
        cache = getattr(dispatcher, "_image_desc_cache", None)
        if cache and file_id in cache:
            cached = cache[file_id]
            cached_desc = cached if isinstance(cached, str) else cached.get("desc", "")
            if cached_desc:
                parts.append("图片：" + _clean_text(cached_desc)[:120])
                has_good_desc = True
        else:
            try:
                from .ai import describe_image
                desc = await describe_image(dispatcher, group_id, file_id, sub_type, summary)
                if desc and desc not in ("[图片]", "[表情/贴纸]"):
                    parts.append("图片：" + _clean_text(desc)[:120])
                    has_good_desc = True
                elif desc:
                    parts.append("图片：" + _clean_text(desc)[:120])
            except Exception:
                log.exception("Image description failed")
    elif summary:
        parts.append("图片：" + summary[:120])

    # Only run OCR if vision API didn't give a good description
    if not has_good_desc:
        image_ref = data.get("url") or file_id
        if image_ref:
            for api_name in ("ocr_image", "ocr_image_enhanced"):
                try:
                    api = getattr(dispatcher.client, api_name)
                    result = await api(image_ref)
                    if result.get("status") != "ok":
                        continue
                    ocr_text = _extract_ocr_text(result.get("data"))
                    if ocr_text:
                        parts.append("OCR文字：" + ocr_text[:180])
                        break
                except Exception:
                    continue
    return "；".join(parts)


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
