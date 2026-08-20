"""Request handling, moderation, notices, and bad-word commands."""

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
from .common import (
    CONFIG_PATH, _commit, _load, _save,
    parse_target_qqs, format_user_label,
)

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def cmd_approve_request(d, group_id, user_id, args, role, sender_card, message):
    flag = args.strip().split(maxsplit=1)[0] if args.strip() else ""
    if not flag:
        await d._reply(group_id, user_id, "这样用：/approve flag")
        return
    from ..request_handler import approve_request
    ok, msg = await approve_request(d, flag, True, "")
    await d._reply(group_id, user_id, msg if ok else "处理失败：" + msg)

async def cmd_reject_request(d, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await d._reply(group_id, user_id, "这样用：/reject flag 原因")
        return
    reason = parts[1] if len(parts) > 1 else "不通过"
    from ..request_handler import approve_request
    ok, msg = await approve_request(d, parts[0], False, reason)
    await d._reply(group_id, user_id, msg if ok else "处理失败：" + msg)

async def cmd_join_review(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    arg = args.strip().lower()
    if arg not in ("on", "off"):
        await d._reply(group_id, user_id, "这样用：/审批 on 或 /审批 off")
        return
    enable = arg == "on"
    gid = str(group_id)
    groups = d.config.setdefault("groups", {})
    group_cfg = groups.setdefault(gid, {})
    group_cfg["join_review"] = enable
    save_group_config(d)
    if enable:
        await d._reply(
            group_id, user_id,
            "入群群内审批已开启：新申请会发到群里，管理员回复「同意/拒绝」即可处理")
    else:
        await d._reply(group_id, user_id, "入群群内审批已关闭")

async def cmd_group_notice(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    text = args.strip()
    if not text or text in ("list", "列表"):
        result = await d.client.get_group_notice(group_id)
        if result.get("status") != "ok":
            await d._reply(group_id, user_id, "公告读取失败：" + str(result.get("msg") or result.get("wording") or result)[:200])
            return
        data = result.get("data", [])
        if isinstance(data, dict):
            data = data.get("notices") or data.get("data") or []
        lines = ["群公告"]
        for item in (data or [])[:5]:
            content = str(item.get("content") or item.get("msg") or item.get("text") or "")[:80].replace("\n", " ")
            nid = item.get("notice_id") or item.get("id") or ""
            if content:
                lines.append(str(nid) + " " + content)
        await d._reply(group_id, user_id, "\n".join(lines) if len(lines) > 1 else "没看到公告")
        return
    result = await d.client.send_group_notice(group_id, text)
    await d._reply(group_id, user_id, "公告发了" if result.get("status") == "ok" else "公告没发成：" + str(result.get("msg") or result.get("wording") or result)[:200])

async def cmd_kick(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    mentions, _ = parse_target_qqs(args, d._extract_mentions(message))
    if not mentions:
        await d._reply(group_id, user_id, "请 @要踢出的人，或写 QQ 号（多个都行）")
        return
    for tid in mentions:
        label = await format_user_label(d, group_id, tid)
        target_ok, target_error = await can_moderate_target(d, group_id, user_id, tid, role)
        if not target_ok:
            await d._reply(group_id, user_id, target_error)
            continue
        r = await d.client.set_group_kick(group_id, tid, False)
        if r.get("status") == "ok":
            log.warning("ADMIN_ACTION actor=%s group=%s action=kick target=%s", user_id, group_id, tid)
            await d._reply(group_id, user_id, "踢掉了：" + label)
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没踢掉 " + label + "，原因是：" + str(err))

async def cmd_ban(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    at_mentions = d._extract_mentions(message)
    mentions, duration_text = parse_target_qqs(args, at_mentions)
    # Known targets must not leak into the duration search (natural-trigger
    # glue passes the target as bare text, e.g. "45 12345").
    for tid in mentions:
        duration_text = re.sub(
            r"(?<!\d)" + re.escape(str(tid)) + r"(?!\d)", " ", duration_text, count=1)
    duration = 30
    # Target QQ numbers are already stripped from duration_text by
    # parse_target_qqs, so a 5-digit QQ is not mistaken for minutes.
    m = re.search(r"(?<!\d)(\d{1,5})(?!\d)(?:\s*(?:分钟|分|min|m))?", duration_text)
    if m:
        duration = max(1, min(int(m.group(1)), 43200))
    elif at_mentions and len(mentions) > len(at_mentions):
        # "@xxx 43200": a trailing bare number after real @-mentions keeps the
        # historic meaning of a duration, not an extra target.
        trailing = mentions[-1]
        if 1 <= trailing <= 43200:
            duration = trailing
            mentions = mentions[:-1]
    if not mentions:
        await d._reply(group_id, user_id, "请 @要禁言的人，或写 QQ 号（多个都行）")
        return
    for tid in mentions:
        label = await format_user_label(d, group_id, tid)
        target_ok, target_error = await can_moderate_target(d, group_id, user_id, tid, role)
        if not target_ok:
            await d._reply(group_id, user_id, target_error)
            continue
        r = await d.client.set_group_ban(group_id, tid, duration * 60)
        if r.get("status") == "ok":
            log.warning("ADMIN_ACTION actor=%s group=%s action=ban target=%s duration=%s",
                        user_id, group_id, tid, duration * 60)
            await d._reply(group_id, user_id, "禁言了：" + label + "，" + str(duration) + " 分钟")
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没禁言成功 " + label + "，原因是：" + str(err))

async def cmd_unban(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    mentions, _ = parse_target_qqs(args, d._extract_mentions(message))
    if not mentions:
        await d._reply(group_id, user_id, "请 @要解禁的人，或写 QQ 号（多个都行）")
        return
    for tid in mentions:
        label = await format_user_label(d, group_id, tid)
        target_ok, target_error = await can_moderate_target(d, group_id, user_id, tid, role)
        if not target_ok:
            await d._reply(group_id, user_id, target_error)
            continue
        r = await d.client.set_group_ban(group_id, tid, 0)
        if r.get("status") == "ok":
            log.warning("ADMIN_ACTION actor=%s group=%s action=unban target=%s", user_id, group_id, tid)
            await d._reply(group_id, user_id, "解开了：" + label)
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没解开 " + label + "，原因是：" + str(err))

async def cmd_allban(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    arg = args.strip().lower()
    if arg not in ("on", "off"):
        await d._reply(group_id, user_id, "这样用：/allban on 或 /allban off")
        return
    enable = arg == "on"
    r = await d.client.call("set_group_whole_ban", {"group_id": group_id, "enable": enable})
    if r.get("status") == "ok":
        await d._reply(group_id, user_id, "全员禁言已经" + ("开了" if enable else "关了"))
    else:
        err = r.get("msg", "") or r.get("wording", "") or str(r)
        await d._reply(group_id, user_id, "没操作成功，原因是：" + str(err))

async def cmd_badword(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    cfg = _load()
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    word = parts[1] if len(parts) > 1 else ""
    gid = str(group_id)
    groups = cfg.setdefault("groups", {})
    group_cfg = groups.setdefault(gid, {"enabled": True, "masters": [], "welcome_msg": {}, "bad_words": {}, "features": {}})
    gcfg = group_cfg
    bw = gcfg.setdefault("bad_words", {
        "enabled": True, "auto_delete": True,
        "warn_msg": "@{user} 请注意文明发言！", "words": [],
    })
    if action == "add" and word:
        if word not in bw["words"]:
            bw["words"].append(word)
            _commit(d, cfg)
            await d._reply(group_id, user_id, "违禁词加好了：" + word)
        else:
            await d._reply(group_id, user_id, "该词已存在")
    elif action == "del" and word:
        if word in bw["words"]:
            bw["words"].remove(word)
            _commit(d, cfg)
            await d._reply(group_id, user_id, "违禁词删掉了：" + word)
        else:
            await d._reply(group_id, user_id, "该词不存在")
    elif action == "on":
        bw["enabled"] = True
        _commit(d, cfg)
        await d._reply(group_id, user_id, "违禁词过滤已开启")
    elif action == "off":
        bw["enabled"] = False
        _commit(d, cfg)
        await d._reply(group_id, user_id, "违禁词过滤已关闭")
    else:
        word_list = ", ".join(bw["words"]) if bw["words"] else "(空)"
        status_text = "开启" if bw["enabled"] else "关闭"
        await d._reply(group_id, user_id,
                       "违禁词列表: " + word_list + "\n状态: " + status_text)
