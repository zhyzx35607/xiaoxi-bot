"""Image, OCR, forwarding, files, music, and media commands."""

import asyncio
import json
import logging
import os
import random
import re
import time

import aiohttp

from ..permission import (
    get_user_level, get_bot_role, get_group_config,
    add_master, remove_master, list_masters,
    save_group_config, can_moderate_target, LEVEL_MASTER, LEVEL_ADMIN,
)
from ..utils import atomic_write_json
from .common import CONFIG_PATH, _load, _save

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def cmd_image_description(d, group_id, user_id, args, role, sender_card, message):
    target_message = message
    replied, _ = await _message_from_reply(d, message)
    if replied:
        target_message = replied.get("message", [])
    image_seg = next((seg for seg in target_message if isinstance(seg, dict) and seg.get("type") == "image"), None)
    if not image_seg:
        await d._reply(group_id, user_id, "请发送图片时带 /图片描述，或者回复图片使用")
        return
    data = image_seg.get("data", {})
    from ..ai import describe_image
    desc = await describe_image(d, group_id, data.get("file", ""), data.get("sub_type", "0"), data.get("summary", ""))
    await d._reply(group_id, user_id, desc or "没有识别出图片内容")

async def cmd_message_reaction(d, group_id, user_id, args, role, sender_card, message):
    message_id = _reply_message_id(message)
    if not message_id:
        await d._reply(group_id, user_id, "请回复一条消息再使用 /表情回应 emoji_id")
        return
    emoji_id = args.strip() or "128077"
    result = await d.client.set_msg_emoji_like(message_id, emoji_id)
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "表情回应失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

def _reply_message_id(message):
    if not isinstance(message, list):
        return 0
    for seg in message:
        if seg.get("type") == "reply":
            data = seg.get("data", {})
            mid = data.get("id") or data.get("message_id")
            try:
                return int(mid)
            except Exception:
                return 0
    return 0

async def _message_from_reply(d, message):
    mid = _reply_message_id(message)
    if not mid:
        return None, 0
    result = await d.client.get_msg(mid)
    if result.get("status") != "ok":
        return None, mid
    return result.get("data", {}), mid

async def cmd_ocr(d, group_id, user_id, args, role, sender_card, message):
    target_message = message
    replied, _ = await _message_from_reply(d, message)
    if replied:
        target_message = replied.get("message", [])
    image_ref = ""
    if isinstance(target_message, list):
        for seg in target_message:
            if seg.get("type") == "image":
                data = seg.get("data", {})
                image_ref = data.get("url") or data.get("file") or data.get("file_id") or ""
                break
    if not image_ref:
        await d._reply(group_id, user_id, "要识别图片的话，发图时带 /ocr，或者回复那张图")
        return
    result = await d.client.ocr_image(image_ref)
    if result.get("status") != "ok":
        result = await d.client.ocr_image_enhanced(image_ref)
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "识别失败：" + str(result.get("msg") or result.get("wording") or result)[:200])
        return
    from ..media import _extract_ocr_text
    text = _extract_ocr_text(result.get("data"))
    await d._reply(group_id, user_id, text or "没识别出文字")

async def cmd_forward_summary(d, group_id, user_id, args, role, sender_card, message):
    target_message = message
    replied, _ = await _message_from_reply(d, message)
    if replied:
        target_message = replied.get("message", [])
    forward_seg = None
    if isinstance(target_message, list):
        for seg in target_message:
            if seg.get("type") == "forward":
                forward_seg = seg
                break
    if not forward_seg:
        await d._reply(group_id, user_id, "要摘要合并转发的话，回复那条转发消息再发 /转发摘要")
        return
    from ..media import describe_forward
    text = await describe_forward(d, forward_seg)
    from ..ai import deepseek_chat
    reply = await deepseek_chat(d, "请把下面这段合并转发内容总结成3-5行，保留关键人物、结论和争议点：\n\n" + text)
    await d._reply(group_id, user_id, reply)

async def cmd_group_files(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个只能在群里用")
        return
    keyword = args.strip().lower()
    result = await d.client.get_group_root_files(group_id)
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "群文件读取失败：" + str(result.get("msg") or result.get("wording") or result)[:200])
        return
    data = result.get("data", {})
    files = data.get("files") if isinstance(data, dict) else []
    folders = data.get("folders") if isinstance(data, dict) else []
    lines = ["群文件"]
    count = 0
    for item in (folders or [])[:10]:
        name = str(item.get("folder_name") or item.get("name") or "文件夹")
        if keyword and keyword not in name.lower():
            continue
        lines.append("[夹] " + name + " id=" + str(item.get("folder_id") or item.get("id") or ""))
        count += 1
    for item in (files or []):
        name = str(item.get("file_name") or item.get("name") or "文件")
        if keyword and keyword not in name.lower():
            continue
        size = item.get("file_size") or item.get("size") or 0
        busid = item.get("busid") or item.get("bus_id") or ""
        file_id = item.get("file_id") or item.get("id") or ""
        lines.append("[文] {name} id={file_id} busid={busid} size={size}".format(
            name=name[:36], file_id=file_id, busid=busid, size=size,
        ))
        count += 1
        if count >= 15:
            break
    if count == 0:
        await d._reply(group_id, user_id, "没找到匹配的群文件")
    else:
        await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_group_file_url(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    parts = args.strip().split()
    if len(parts) < 2:
        await d._reply(group_id, user_id, "这样用：/文件链接 file_id busid")
        return
    result = await d.client.get_group_file_url(group_id, parts[0], parts[1])
    if result.get("status") == "ok":
        data = result.get("data", {})
        url = data.get("url") or data.get("download_url") or str(data)
        await d._reply(group_id, user_id, str(url)[:1000])
    else:
        await d._reply(group_id, user_id, "获取失败：" + str(result.get("msg") or result.get("wording") or result)[:200])

async def cmd_essence_list(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    result = await d.client.get_essence_msg_list(group_id)
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "读取精华失败：" + str(result.get("msg") or result.get("wording") or result)[:200])
        return
    rows = result.get("data", [])
    if not rows:
        await d._reply(group_id, user_id, "这个群还没有精华消息")
        return
    lines = ["群精华"]
    for item in rows[:10]:
        sender = item.get("sender_nick") or item.get("sender_id") or item.get("sender") or "未知"
        mid = item.get("message_id") or item.get("msg_id") or ""
        content = str(item.get("content") or item.get("message") or "")[:50].replace("\n", " ")
        lines.append(str(mid) + " " + str(sender) + " " + content)
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_group_honor(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    result = await d.client.get_group_honor_info(group_id, "all")
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "群荣誉读取失败：" + str(result.get("msg") or result.get("wording") or result)[:200])
        return
    data = result.get("data", {})
    lines = ["群荣誉"]
    for key, title in (("talkative_list", "龙王"), ("performer_list", "群聊之火"), ("legend_list", "群聊炽焰"), ("strong_newbie_list", "冒尖小春笋")):
        values = data.get(key) or []
        if values:
            names = []
            for item in values[:3]:
                names.append(str(item.get("nickname") or item.get("user_id") or item))
            lines.append(title + "：" + "、".join(names))
    current = data.get("current_talkative")
    if isinstance(current, dict) and current:
        lines.append("当前龙王：" + str(current.get("nickname") or current.get("user_id")))
    await d._reply(group_id, user_id, "\n".join(lines) if len(lines) > 1 else "暂时没拿到群荣誉数据")

async def cmd_mark_read(d, group_id, user_id, args, role, sender_card, message):
    mid = _reply_message_id(message)
    if mid:
        result = await d.client.mark_msg_as_read(mid)
    elif group_id:
        result = await d.client.mark_group_msg_as_read(group_id)
    else:
        result = await d.client.mark_all_as_read()
    await d._reply(group_id, user_id, "标记好了" if result.get("status") == "ok" else "标记失败：" + str(result)[:160])

async def cmd_set_essence(d, group_id, user_id, args, role, sender_card, message):
    mid = _reply_message_id(message)
    if not mid:
        await d._reply(group_id, user_id, "回复一条消息再发 /精华")
        return
    result = await d.client.set_essence_msg(mid)
    log.info("set_essence_msg completed: mid=%s status=%s", mid, result.get("status"))
    await d._reply(group_id, user_id, "设成精华了" if result.get("status") == "ok" else "没设成：" + str(result.get("msg") or result.get("wording") or result)[:200])

async def cmd_delete_essence(d, group_id, user_id, args, role, sender_card, message):
    mid = _reply_message_id(message)
    if not mid and args.strip().isdigit():
        mid = int(args.strip())
    if not mid:
        await d._reply(group_id, user_id, "回复精华消息或写消息ID：/删精华 123")
        return
    result = await d.client.delete_essence_msg(mid)
    await d._reply(group_id, user_id, "删掉了" if result.get("status") == "ok" else "没删掉：" + str(result.get("msg") or result.get("wording") or result)[:200])

async def handle_music_search(d, group_id, user_id, raw_text, sender_card):
    keyword = None
    for pfx in ["我要点歌", "我想点歌", "帮我点歌", "点一下歌", "点歌", "点首", "来首", "放首", "搜歌"]:
        if raw_text.startswith(pfx):
            keyword = raw_text[len(pfx):].strip()
            if keyword:
                break
    # Also handle "点歌 xx" without space
    if not keyword:
        import re as _re_ms
        m = _re_ms.match(r"点歌\s*(.+)", raw_text)
        if m:
            keyword = m.group(1).strip()
    if not keyword:
        return False
    try:
        session = d.client.session
        url = "https://music.163.com/api/search/get?s=" + keyword + "&type=1&limit=1"
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                text = await resp.text()
                data = json.loads(text)
            else:
                data = None
    except Exception as e:
        log.error("Music search exception: %s", e)
        data = None
    if data:
        try:
            songs = data.get("result", {}).get("songs", [])
            if songs:
                song = songs[0]
                song_id = song["id"]
                music_msg = [{"type": "music", "data": {"type": "163", "id": str(song_id)}}]
                r = await d.client.call("send_group_msg", {"group_id": group_id, "message": music_msg})
                if r.get("status") != "ok":
                    log.warning(
                        "Music card send failed: status=%s retcode=%s",
                        r.get("status"), r.get("retcode"),
                    )
                return True
        except Exception as e:
            log.error("Music parse error: %s", e)
    from ..ai import deepseek_chat
    reply = await deepseek_chat(d, "用户想点歌「" + keyword + "」，请用1行推荐一首歌（格式：推荐「歌名 - 歌手」）。不确定就诚实说。")
    await d.client.send_group_msg(group_id, reply)
    return True

async def cmd_forward_msg(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个只能在群里用")
        return
    mid = _reply_message_id(message)
    if not mid:
        await d._reply(group_id, user_id, "请回复一条消息再发 /转发")
        return
    r = await d.client.forward_group_single_msg(group_id, mid)
    if r.get("status") == "ok":
        await d._reply(group_id, user_id, "转发成功")
    else:
        err = r.get("msg", "") or r.get("wording", "") or str(r)
        await d._reply(group_id, user_id, "转发失败：" + str(err)[:200])

async def cmd_set_group_avatar(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个只能在群里用")
        return
    target_message = message
    replied, _ = await _message_from_reply(d, message)
    if replied:
        target_message = replied.get("message", [])
    image_url = ""
    if isinstance(target_message, list):
        for seg in target_message:
            if seg.get("type") == "image":
                data = seg.get("data", {})
                image_url = data.get("url") or data.get("file") or ""
                break
    if not image_url:
        await d._reply(group_id, user_id, "请回复一张图片再发 /setgroupavatar")
        return
    r = await d.client.set_group_portrait(group_id, image_url)
    if r.get("status") == "ok":
        await d._reply(group_id, user_id, "群头像已更新")
    else:
        err = r.get("msg", "") or r.get("wording", "") or str(r)
        await d._reply(group_id, user_id, "设置失败：" + str(err)[:200])

async def cmd_generate_image(d, group_id, user_id, args, role, sender_card, message):
    """Generate an image using Agnes AI."""
    text = args.strip()
    if not text:
        await d._reply(group_id, user_id, "这样用：/生图 提示词\n例：/生图 一只在草地上跑的橘猫")
        return
    from ..ai import generate_image
    url, err = await generate_image(d, text)
    if url:
        # Send as image segment
        img_seg = [{"type": "image", "data": {"file": url}}]
        try:
            if group_id:
                await d.client.send_group_msg(group_id, img_seg)
            else:
                await d.client.send_private_msg(user_id, img_seg)
        except Exception as e:
            log.error("Failed to send generated image: %s", e)
            await d._reply(group_id, user_id, f"图片生成成功但发送失败: {str(e)[:100]}")
    else:
        await d._reply(group_id, user_id, err or "生图失败，请稍后重试")
