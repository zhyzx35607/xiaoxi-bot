"""Sticker collection, analysis, inventory, and image description."""

import asyncio
import json
import logging
import os
import random
import time

import aiohttp

from ..utils import atomic_write_json
from .providers import _call_vision_api, _get_semaphore, _get_vision_api_key

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STICKER_DIR = os.path.join(_ROOT, "data", "stickers")
os.makedirs(STICKER_DIR, exist_ok=True)

_STICKER_LAST_SENT = {}

_STICKER_DAILY_COUNT = {}


def _load_sticker_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            stickers = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        log.warning("Sticker inventory load failed: %s", error)
        return []
    if not isinstance(stickers, list):
        log.warning("Sticker inventory root is not a list")
        return []
    valid = [item for item in stickers if isinstance(item, dict)]
    if len(valid) != len(stickers):
        log.warning("Sticker inventory contained invalid entries")
    return valid

def _allow_sticker_send(config, group_id, user_id):
    """Resource/spam boundary only; AI still decides whether a sticker fits."""
    cfg = config.get("sticker_mode", {})
    now = time.time()
    key = "g:{}".format(group_id) if group_id else "u:{}".format(user_id)
    cooldown = int(cfg.get("group_cooldown_seconds", 180) if group_id
                   else cfg.get("private_cooldown_seconds", 90))
    if now - _STICKER_LAST_SENT.get(key, 0) < cooldown:
        return False
    day_key = time.strftime("%Y%m%d") + ":" + key
    limit = int(cfg.get("daily_send_limit", 12))
    if _STICKER_DAILY_COUNT.get(day_key, 0) >= limit:
        return False
    _STICKER_LAST_SENT[key] = now
    _STICKER_DAILY_COUNT[day_key] = _STICKER_DAILY_COUNT.get(day_key, 0) + 1
    if len(_STICKER_DAILY_COUNT) > 500:
        today = time.strftime("%Y%m%d") + ":"
        for item in list(_STICKER_DAILY_COUNT):
            if not item.startswith(today):
                _STICKER_DAILY_COUNT.pop(item, None)
    return True

async def describe_image(dispatcher, group_id, file_id, sub_type, summary=""):
    """Describe image content. Vision API (Qwen) first, QQ summary as fallback."""
    config = dispatcher.config
    import html as _html
    # Decode QQ summary for potential fallback use
    qq_summary = ""
    if summary:
        qq_summary = _html.unescape(summary).strip()
    # Try vision API first
    image_url = None
    try:
        result = await dispatcher.client.call("get_image", {"file": file_id})
        if result.get("status") == "ok":
            data = result.get("data", {})
            image_url = data.get("url") or data.get("file")
    except Exception as e:
        log.error("get_image failed: %s", e)
    if image_url:
        log.info("Vision API sticker analysis started")
        desc = await _call_vision_api(config, image_url, session=dispatcher.client.session)
        if desc:
            log.info("Vision API sticker analysis completed")
            return desc
    # Fallback: use QQ summary if vision API failed or image URL unavailable
    if qq_summary:
        log.info("Sticker analysis used QQ summary fallback")
        return qq_summary
    # Ultimate fallback
    if sub_type and str(sub_type) != "0":
        return "[表情/贴纸]"
    return "[图片]"

async def collect_sticker_async(dispatcher, group_id, file_id, sub_type, summary="",
                                    is_private=False):
    """Collect sticker with AI vision analysis. Called from dispatcher."""
    sticker_cfg = dispatcher.config.get("sticker_mode", {})
    if not sticker_cfg.get("collect", True):
        return
    # Only collect real stickers/emoji (sub_type != "0"); skip normal photos.
    if str(sub_type or "0") == "0":
        return
    prefix = "private" if is_private else "group"
    path = os.path.join(STICKER_DIR, f"{prefix}_{group_id}.json")
    stickers = []
    if os.path.exists(path):
        stickers = await asyncio.to_thread(_load_sticker_file, path)
    # Avoid duplicates
    if any(s.get("file") == file_id for s in stickers):
        return
    # Group chat sampling: only collect ~30% to avoid overload (private chat collects all)
    if not is_private and random.random() > 0.3:
        return
    desc_text = ""
    emotion = ""
    tags = []
    usage_scene = ""
    if summary:
        import html as _html_st
        desc_text = _html_st.unescape(summary)[:50]
    # Reuse dispatcher image cache if available (avoid duplicate vision API call)
    cached_entry = None
    img_cache = getattr(dispatcher, "_image_desc_cache", None)
    if img_cache and file_id in img_cache:
        entry = img_cache[file_id]
        cached_entry = entry if isinstance(entry, dict) else {"desc": str(entry)}
    # Always try vision API for new stickers (free quota, one-time cost)
    image_url = None
    if not desc_text:
        try:
            result = await dispatcher.client.call("get_image", {"file": file_id})
            if result.get("status") == "ok":
                data = result.get("data", {})
                image_url = data.get("url") or data.get("file")
        except Exception as error:
            log.debug("Sticker image lookup failed: %s", error)
    # Call vision API for detailed analysis (or use cached description)
    if cached_entry and not desc_text:
        desc_text = cached_entry.get("desc", "")[:50]
        emotion = cached_entry.get("emotion", "")
        tags = cached_entry.get("tags", [])
        usage_scene = cached_entry.get("usage", "")
    elif image_url:
        result = await _analyze_sticker_vision(dispatcher.config, image_url,
                                               session=dispatcher.client.session)
        if result:
            # Parse structured response: description|emotion|tags|usage
            parts = result.split("|")
            if len(parts) >= 1:
                desc_text = parts[0].strip()
            if len(parts) >= 2:
                emotion = parts[1].strip()
            if len(parts) >= 3:
                tags = [t.strip() for t in parts[2].split(",") if t.strip()]
            if len(parts) >= 4:
                usage_scene = parts[3].strip()
    stickers.append({
        "file": file_id,
        "sub_type": sub_type,
        "desc": desc_text,
        "emotion": emotion,
        "tags": tags,
        "usage": usage_scene,
        "group_id": f"private_{group_id}" if is_private else str(group_id),
        "ts": time.time()
    })
    # Keep at most sticker_mode.max_stickers entries
    max_stickers = int(sticker_cfg.get("max_stickers", 50))
    if len(stickers) > max_stickers:
        stickers = stickers[-max_stickers:]
    await asyncio.to_thread(atomic_write_json, path, stickers)
    log.info("Sticker collected and analyzed: emotion=%s", emotion or "unknown")

async def _analyze_sticker_vision(config, image_url, session=None):
    """Use vision API to analyze sticker: description, tags, category, usage."""
    runtime = config.get("runtime", {})
    async with _get_semaphore("vision", runtime.get("vision_concurrency", 1)):
        return await _analyze_sticker_vision_inner(config, image_url, session)

async def _analyze_sticker_vision_inner(config, image_url, session=None):
    """Use vision API to analyze sticker: description, tags, category, usage."""
    vision_cfg = config.get("vision_api", {})
    if not vision_cfg:
        return None
    api_key = _get_vision_api_key(config)
    if not api_key:
        return None
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json"
    }
    prompt = (
        "请描述这张表情包。用以下格式回复（严格4段，用|分隔）：\n"
        "简短描述(15字内)|情绪标签|关键词1,关键词2|适用场景(10字内)\n"
        "情绪标签必须从以下选一个：开心 伤心 生气 无语 惊讶 害羞 尴尬 得意 困惑 拒绝 赞同 嘲讽 感谢 安慰 庆祝 卖萌 敷衍 打招呼 告别 晚安 点赞\n"
        "示例：猫翻白眼|无语|翻白眼,猫|对无语的事表示同感"
    )
    payload = {
        "model": vision_cfg.get("model", "qwen-vl-plus"),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        }],
        "max_tokens": 100,
        "temperature": 0.3,
    }
    url = vision_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1") + "/chat/completions"
    async def _do_post(sess):
        async with sess.post(url, headers=headers, json=payload,
                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
            else:
                body = await resp.text()
                log.warning("Vision API sticker returned %d: %s", resp.status, body[:150])
    try:
        if session:
            return await _do_post(session)
        async with aiohttp.ClientSession() as s:
            return await _do_post(s)
    except Exception as e:
        log.error("Sticker vision analysis failed: %s", e)
    return None

def get_sticker_summaries(group_id):
    """Get sticker info for /list command."""
    path = os.path.join(STICKER_DIR, f"group_{group_id}.json")
    if not os.path.exists(path):
        return []
    stickers = _load_sticker_file(path)
    summaries = []
    for s in stickers:
        desc = s.get("desc") or s.get("description") or s.get("summary", "") or "无描述"
        emotion = s.get("emotion", "")
        tags = s.get("tags", [])
        usage = s.get("usage", "")
        line = desc
        if emotion:
            line += " [" + emotion + "]"
        if tags:
            line += " [" + ",".join(tags[:3]) + "]"
        if usage:
            line += " - " + usage
        summaries.append({"description": desc, "emotion": emotion, "tags": tags, "usage": usage,
                          "display": line})
    return summaries

def _build_sticker_inventory(group_id=None, user_id=None, is_private=False):
    """Build sticker inventory summary by emotion for system prompt.
    Tells AI what stickers are available so it can decide whether to use [STICKER:xxx]."""
    gid = user_id if is_private else group_id
    if not gid:
        return ""
    prefix = "private" if is_private else "group"
    path = os.path.join(STICKER_DIR, f"{prefix}_{gid}.json")
    if not os.path.exists(path):
        return ""
    stickers = _load_sticker_file(path)
    if not stickers:
        return ""
    # Group by emotion, collect up to 2 descriptions per emotion
    by_emotion = {}
    for s in stickers:
        em = s.get("emotion", "")
        if not em:
            tags = s.get("tags", [])
            em = tags[0] if tags else "其他"
        if em not in by_emotion:
            by_emotion[em] = []
        desc = s.get("desc") or s.get("description", "") or ""
        if desc and desc not in by_emotion[em]:
            by_emotion[em].append(desc[:10])
    total = len(stickers)
    lines = []
    for em in sorted(by_emotion):
        samples = by_emotion[em][:2]
        count = len(by_emotion[em])
        lines.append(f"{em}({count}): " + "、".join(samples))
    summary = "\n".join(lines)
    return (f"【你收藏的表情包（共{total}个）】\n{summary}\n"
            "回复时如果觉得发个表情包能更好表达情绪，在末尾加 [STICKER:情绪标签]。")
