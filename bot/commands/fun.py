"""Fun, social, and lightweight interaction commands."""

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

async def cmd_saying(d, group_id, user_id, args, role, sender_card, message):
    """/一言 — random quote via uapis.cn."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user", path="/saying"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    data = await _uapi.uapi_get(d, "/saying", kind="user")
    if not data:
        await d._reply(group_id, user_id, "没取到一言，等会再试")
        return
    text = str(data.get("content") or "").strip()
    author = str(data.get("author") or "").strip()
    await d._reply(group_id, user_id,
                   text + ("\n—— " + author if author else ""))

async def cmd_answerbook(d, group_id, user_id, args, role, sender_card, message):
    """/答案之书 [问题] — answer book via uapis.cn."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user", path="/answerbook/ask"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    question = args.strip()[:60] or "今天会发生什么"
    data = await _uapi.uapi_get(d, "/answerbook/ask",
                                params={"question": question}, kind="user")
    if not data:
        await d._reply(group_id, user_id, "答案之书今天不想说话，等会再问")
        return
    await d._reply(group_id, user_id,
                   "问：{}\n答：{}".format(question, data.get("answer", "?")))

async def cmd_poke_user(d, group_id, user_id, args, role, sender_card, message):
    mentions = d._extract_mentions(message) if group_id else []
    target = mentions[0] if mentions else (int(args.strip()) if args.strip().isdigit() else user_id)
    result = await (d.client.group_poke(group_id, target) if group_id else d.client.friend_poke(target))
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "戳一戳失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_like(d, group_id, user_id, args, role, sender_card, message):
    target = user_id
    if args.strip():
        try:
            target = int(args.strip())
        except ValueError:
            pass
    mentions = d._extract_mentions(message)
    if mentions:
        target = mentions[0]
    today = time.strftime("%Y%m%d")
    key = today + ":" + str(target)
    if key in d._daily_likes:
        return
    times = 10
    r = await d.client.send_like(target, times)
    if r.get("status") == "ok":
        d._daily_likes[key] = True
        d.save_runtime_state(force=True)
        await d._reply(group_id, user_id, "点好了，给 " + str(target) + " 赞了 " + str(times) + " 下")
    else:
        err = r.get("msg", "") or r.get("wording", "") or str(r)
        await d._reply(group_id, user_id, "没点上，原因是：" + str(err))

async def cmd_rank(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    counts = d._group_msg_counts.get(group_id, {})
    if not counts:
        await d._reply(group_id, user_id, "暂时还没记到发言")
        return
    sorted_users = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["近期发言排行", ""]
    medals = ["第一", "第二", "第三"] + ["上榜"] * 7
    for i, (uid, cnt) in enumerate(sorted_users):
        try:
            info = await d.client.get_group_member_info(group_id, uid)
            name = str(uid)
            if info.get("status") == "ok":
                name = info.get("data", {}).get("card") or info.get("data", {}).get("nickname", str(uid))
        except Exception:
            name = str(uid)
        lines.append("  " + medals[i] + "  " + name + "  " + str(cnt) + " 条")
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_fortune(d, group_id, user_id, args, role, sender_card, message):
    today = time.strftime("%Y%m%d")
    key = today + ":" + str(user_id)
    if key in d._daily_fortunes:
        await d._reply(group_id, user_id, "今天已经看过啦，明天再来")
        return
    d._daily_fortunes[key] = True
    d.save_runtime_state(force=True)
    from ..ai import deepseek_chat
    prompt = ("请为星座运势生成一段今日运势，包含综合运势、爱情运势、工作/学业运，"
              "每项一句话，语气像普通群友，简短4-5行即可。")
    reply = await deepseek_chat(d, prompt)
    if reply:
        await d._reply(group_id, user_id, sender_card + " 的今日运势\n\n" + reply)
    else:
        await d._reply(group_id, user_id, "脑子卡了一下，等会再试")

async def cmd_profile_like(d, group_id, user_id, args, role, sender_card, message):
    r = await d.client.get_profile_like()
    log.info("get_profile_like response: %s", str(r)[:300])
    if r.get("status") != "ok":
        await d._reply(group_id, user_id, "获取点赞信息失败：" + str(r.get("msg") or r)[:200])
        return
    data = r.get("data", {})
    # NapCat 可能返回不同结构，兼容处理
    favorite = data.get("favoriteInfo") if isinstance(data.get("favoriteInfo"), dict) else {}
    total = (
        data.get("total_like_count") or data.get("like_count") or data.get("total") or
        favorite.get("total_count") or 0
    )
    recent = (
        data.get("like_received_7days") or data.get("recent_like_count") or
        data.get("recent") or favorite.get("today_count") or 0
    )
    recent_label = "近7天收到"
    if not (data.get("like_received_7days") or data.get("recent_like_count") or data.get("recent")) and favorite:
        recent_label = "今日收到"
    lines = [
        f"总点赞数: {total}",
        f"{recent_label}: {recent}",
    ]
    await d._reply(group_id, user_id, "\n".join(lines))
