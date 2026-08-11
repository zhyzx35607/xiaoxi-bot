"""Additional safe UApiS query commands."""

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlparse


def _json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) if data is not None else ""


def _safe_public_url(value):
    try:
        parsed = urlparse(str(value or "").strip())
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return ""
        try:
            if parsed.port not in (None, 80, 443):
                return ""
        except ValueError:
            return ""
        host = parsed.hostname.lower()
        if host in ("localhost",) or host.endswith(".local"):
            return ""
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return ""
        except ValueError:
            pass
        url = parsed._replace(fragment="").geturl()
        return url if len(url) <= 2048 else ""
    except Exception:
        return ""


def _image_url(args, message):
    direct = _safe_public_url(args)
    if direct:
        return direct
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "image":
                continue
            data = segment.get("data") or {}
            for key in ("url", "file"):
                value = _safe_public_url(data.get(key))
                if value:
                    return value
    return ""


async def _resolved_public_url(value):
    url = _safe_public_url(value)
    if not url:
        return ""
    parsed = urlparse(url)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError):
        return ""
    if not addresses:
        return ""
    for item in addresses:
        try:
            address = ipaddress.ip_address(item[4][0])
        except (ValueError, IndexError, TypeError):
            return ""
        if not address.is_global:
            return ""
    return url


async def _query(d, group_id, user_id, path, *, params=None, body=None,
                 form=None, title="查询结果"):
    from .. import uapi
    if not uapi.credits_available(d.config, "user", path=path):
        await d._reply(group_id, user_id, "今天的查询积分用完啦，明天再来吧")
        return
    if form is not None:
        data = await uapi.uapi_post_form(d, path, form, kind="user")
    elif body is not None:
        data = await uapi.uapi_post(d, path, json_body=body, kind="user")
    else:
        data = await uapi.uapi_get(d, path, params=params, kind="user")
    if data is None:
        await d._reply(group_id, user_id, "这次没查到，等会再试一下吧")
        return
    await d._reply(group_id, user_id, _json_text(data), title=title)


async def cmd_qrcode(d, group_id, user_id, args, role, sender_card, message):
    text = args.strip()
    if not text:
        await d._reply(group_id, user_id, "用法：/二维码 要编码的文字或链接")
        return
    from .queries import _send_uapi_image
    await _send_uapi_image(
        d, group_id, user_id, "/image/qrcode",
        {"text": text[:1000], "size": 512, "format": "image"}, "二维码")


async def cmd_holiday(d, group_id, user_id, args, role, sender_card, message):
    value = args.strip()
    params = {"timezone": "Asia/Shanghai", "include_nearby": "true"}
    if value:
        key = "date" if len(value) == 10 else ("month" if len(value) == 7 else "year")
        params[key] = value
    await _query(d, group_id, user_id, "/misc/holiday-calendar",
                 params=params, title="节假日与万年历")


async def cmd_daily_word(d, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split()
    category = parts[0].lower() if parts else "all"
    if category not in ("all", "cet4", "cet6", "ielts", "toefl", "gre"):
        await d._reply(group_id, user_id, "词库支持：all、cet4、cet6、ielts、toefl、gre")
        return
    count = min(10, max(1, int(parts[1]))) if len(parts) > 1 and parts[1].isdigit() else 1
    await _query(d, group_id, user_id, "/daily/word",
                 params={"category": category, "count": count, "example": "true",
                         "phonetic": "true"}, title="每日单词")


async def cmd_github_lookup(d, group_id, user_id, args, role, sender_card, message):
    target = args.strip().removeprefix("https://github.com/").strip("/")
    if not target:
        await d._reply(group_id, user_id, "用法：/github 用户名 或 /github owner/repo")
        return
    if "/" in target:
        await _query(d, group_id, user_id, "/github/repo",
                     params={"repo": target[:160]}, title="GitHub 仓库")
    else:
        await _query(d, group_id, user_id, "/github/user",
                     params={"user": target[:80], "pinned": "true", "repos": "true"},
                     title="GitHub 用户")


async def cmd_url_status(d, group_id, user_id, args, role, sender_card, message):
    url = await _resolved_public_url(args)
    if not url:
        await d._reply(group_id, user_id, "只支持公开的 http/https 地址，内网地址不查")
        return
    await _query(d, group_id, user_id, "/network/urlstatus",
                 params={"url": url}, title="网址状态")


async def cmd_sensitive_analyze(d, group_id, user_id, args, role, sender_card, message):
    keywords = [item.strip() for item in args.replace("，", ",").split(",") if item.strip()]
    if not keywords:
        await d._reply(group_id, user_id, "用法：/敏感词 词1,词2")
        return
    await _query(d, group_id, user_id, "/sensitive-word/analyze",
                 body={"keywords": keywords[:100]}, title="敏感词分析")


async def cmd_bili_live(d, group_id, user_id, args, role, sender_card, message):
    value = args.strip()
    if not value:
        await d._reply(group_id, user_id, "用法：/b站直播 UID，直播间号请写 room:房间号")
        return
    key = "room_id" if value.lower().startswith("room:") else "mid"
    value = value.split(":", 1)[-1]
    await _query(d, group_id, user_id, "/social/bilibili/liveroom",
                 params={key: value}, title="B站直播间")


async def cmd_bili_user(d, group_id, user_id, args, role, sender_card, message):
    uid = args.strip()
    if not uid.isdigit():
        await d._reply(group_id, user_id, "用法：/b站用户 UID")
        return
    await _query(d, group_id, user_id, "/social/bilibili/userinfo",
                 params={"uid": uid}, title="B站用户")


async def cmd_bili_replies(d, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split()
    if not parts or not parts[0].isdigit():
        await d._reply(group_id, user_id, "用法：/b站评论 OID [数量]")
        return
    count = min(20, max(1, int(parts[1]))) if len(parts) > 1 and parts[1].isdigit() else 5
    await _query(d, group_id, user_id, "/social/bilibili/replies",
                 params={"oid": parts[0], "sort": "hot", "ps": str(count), "pn": "1"},
                 title="B站评论")


async def cmd_cloud_ocr(d, group_id, user_id, args, role, sender_card, message):
    url = await _resolved_public_url(_image_url(args, message))
    if not url:
        await d._reply(group_id, user_id, "用法：/云OCR 公开图片URL，或发送带URL的图片")
        return
    await _query(d, group_id, user_id, "/image/ocr",
                 form={"url": url, "need_location": "false", "return_markdown": "true"},
                 title="云端 OCR")


async def cmd_nsfw_check(d, group_id, user_id, args, role, sender_card, message):
    url = await _resolved_public_url(_image_url(args, message))
    if not url:
        await d._reply(group_id, user_id, "用法：/图片审核 公开图片URL")
        return
    await _query(d, group_id, user_id, "/image/nsfw",
                 form={"url": url}, title="图片安全检测")
