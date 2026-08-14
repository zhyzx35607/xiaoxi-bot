"""Configuration, group administration, and owner commands."""

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
from .common import CONFIG_PATH, _commit, _load, _save, resolve_scoped_group_targets

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def cmd_at_all(d, group_id, user_id, args, role, sender_card, message):
    """/全体 内容 — @everyone (admin+, bot must be admin/owner)."""
    if not group_id:
        await d._reply(None, user_id, "这个只能在群里用")
        return
    content = args.strip()
    if not content:
        await d._reply(group_id, user_id, "这样用：/全体 要说的内容")
        return
    remain = await d.client.get_group_at_all_remain(group_id)
    rdata = remain.get("data") or {}
    if remain.get("status") == "ok" and isinstance(rdata, dict) \
            and rdata and not rdata.get("can_at_all", True):
        await d._reply(group_id, user_id, "今天@全体的次数用完了，明天再来")
        return
    await d.client.send_group_msg(group_id, [
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": " " + content[:200]}},
    ])
    log.info("AT_ALL group=%s user=%s chars=%s", group_id, user_id, len(content))

async def _toggle_group_feature(d, group_id, user_id, args, feature, label, cmd_name):
    """Shared on/off toggle for per-group feature flags."""
    if not group_id:
        await _toggle_groups_feature(d, user_id, args, feature, label, cmd_name)
        return
    arg = args.strip().lower()
    cfg = _load()
    groups = cfg.setdefault("groups", {})
    group_cfg = groups.setdefault(str(group_id), {"enabled": True, "masters": [],
                                                  "welcome_msg": {}, "bad_words": {},
                                                  "features": {}})
    feats = group_cfg.setdefault("features", {})
    current = feats.get(feature, True)
    if arg not in ("on", "off"):
        await d._reply(group_id, user_id,
                       "本群{}：{}\n用法：/{} on 或 /{} off".format(
                           label, "开启" if current else "关闭", cmd_name, cmd_name))
        return
    feats[feature] = (arg == "on")
    _commit(d, cfg)
    await d._reply(group_id, user_id,
                   "本群{}已{}".format(label, "开启" if feats[feature] else "关闭"))

async def _toggle_groups_feature(d, user_id, args, feature, label, cmd_name):
    """Private cross-group batch toggle: /cmd 群号1 群号2|all on|off."""
    usage = ("私聊用法：/{} 群号 on|off，多个群号用空格或逗号分开，"
             "all 表示全部已配置群".format(cmd_name))
    tokens = [t for t in re.split(r"[\s,，]+", (args or "").strip()) if t]
    action = tokens[-1].lower() if tokens else ""
    if action not in ("on", "off"):
        if len(tokens) == 1 and tokens[0].isdigit():
            # Single-group status query stays compatible: /cmd 群号
            cfg = _load()
            groups = cfg.get("groups", {})
            current = groups.get(tokens[0], {}).get("features", {}).get(feature, True)
            await d._reply(None, user_id,
                           "群{}的{}：{}\n{}".format(tokens[0], label,
                                                    "开启" if current else "关闭", usage))
        else:
            await d._reply(None, user_id, usage)
        return
    group_tokens = tokens[:-1]
    if not group_tokens:
        await d._reply(None, user_id, usage)
        return
    cfg = _load()
    groups = cfg.setdefault("groups", {})
    if any(t.lower() == "all" for t in group_tokens):
        targets = list(groups.keys())
    elif all(t.isdigit() for t in group_tokens):
        targets = list(dict.fromkeys(group_tokens))
    else:
        await d._reply(None, user_id, "群号只能是数字或 all\n" + usage)
        return
    enabled = action == "on"
    applied, skipped = [], []
    for gid in targets:
        group_cfg = groups.get(gid)
        if not isinstance(group_cfg, dict):
            skipped.append(gid)
            continue
        group_cfg.setdefault("features", {})[feature] = enabled
        applied.append(gid)
    if applied:
        _commit(d, cfg)
    lines = []
    if applied:
        lines.append("已对 {} 个群{}{}：{}".format(
            len(applied), "开启" if enabled else "关闭", label, "、".join(applied)))
    if skipped:
        lines.append("跳过未配置群：" + "、".join(skipped))
    if not lines:
        lines.append("没有可操作的已配置群")
    await d._reply(None, user_id, "\n".join(lines))

async def cmd_acg_switch(d, group_id, user_id, args, role, sender_card, message):
    """/acg图 on|off — toggle scheduled ACG image push for this group."""
    await _toggle_group_feature(d, group_id, user_id, args,
                                "acg_images", "每日ACG图推送", "acg图")

async def cmd_hotboard_switch(d, group_id, user_id, args, role, sender_card, message):
    """/热榜推送 on|off — toggle scheduled hot-board push for this group."""
    await _toggle_group_feature(d, group_id, user_id, args,
                                "hotboard_push", "每日热榜推送", "热榜推送")

async def cmd_bili_parse_switch(d, group_id, user_id, args, role, sender_card, message):
    """/b站解析 on|off — toggle auto B站 video parse for this group."""
    await _toggle_group_feature(d, group_id, user_id, args,
                                "bili_parse", "B站视频自动解析", "b站解析")

async def cmd_touchgal_switch(d, group_id, user_id, args, role, sender_card, message):
    """/gal资源 on|off — toggle automatic TouchGal replies for this group."""
    await _toggle_group_feature(d, group_id, user_id, args,
                                "galgame_resource", "Galgame资源自动回复", "gal资源")

async def cmd_touchgal(d, group_id, user_id, args, role, sender_card, message):
    from ..touchgal import _settings, parse_command_query, search_and_format

    query = args.strip()
    if query.lower() in ("status", "状态"):
        settings = _settings(d)
        await d._reply(
            group_id, user_id,
            "TouchGal：{}\nToken：{}\n自动回复：{}\n用法：/gal 作品名 [平台]"
            .format(
                "已启用" if settings["enabled"] else "已关闭",
                "已配置" if settings["token"] else "未配置",
                "已开启" if settings["auto_reply"] else "已关闭",
            ),
        )
        return
    if not query:
        await d._reply(
            group_id, user_id,
            "用法：/gal 作品名 [平台]\n例如：/gal 千恋万花 安卓\n查看状态：/gal status",
        )
        return
    parsed = parse_command_query(query)
    if not parsed["title"]:
        await d._reply(group_id, user_id, "没有识别到作品名，请使用 /gal 作品名 [平台]")
        return
    result = await search_and_format(
        d, parsed["title"], platform=parsed["platform"], explicit=True,
    )
    await d._reply(group_id, user_id, result.get("text") or "TouchGal 查询失败")

def _parse_mid(text):
    """Accept a bare mid or a space.bilibili.com URL; return int mid or 0."""
    import re as _re_mid
    m = _re_mid.search(r"space\.bilibili\.com/(\d+)", text or "")
    if m:
        return int(m.group(1))
    m = _re_mid.search(r"\b(\d{3,12})\b", text or "")
    return int(m.group(1)) if m else 0

async def cmd_bili_push(d, group_id, user_id, args, role, sender_card, message):
    """/b站推送 add|del|list — manage watched UP主 for this group (master+)."""
    usage = ("用法：\n"
             "/b站推送 add <mid或空间链接> — 盯一个UP主\n"
             "/b站推送 del <mid> — 不盯了\n"
             "/b站推送 list — 看本群在盯谁\n"
             "mid 就是UP主空间网址 space.bilibili.com/ 后面的数字")
    parts = args.strip().split()
    action = parts[0].lower() if parts else "list"
    target_group = group_id
    index = 1
    if not group_id:
        if len(parts) >= 2 and parts[1].isdigit():
            target_group = int(parts[1])
            index = 2
        else:
            await d._reply(None, user_id,
                           "私聊跨群这样用：/b站推送 add <群号> <mid或空间链接>\n" + usage)
            return
    if not target_group:
        await d._reply(group_id, user_id, "要带上群号，不然我不知道改哪个群")
        return
    cfg = _load()
    groups = cfg.setdefault("groups", {})
    group_cfg = groups.setdefault(str(target_group), {"enabled": True, "masters": [],
                                                      "welcome_msg": {}, "bad_words": {},
                                                      "features": {}})
    push_cfg = group_cfg.setdefault("bili_push", {"mids": []})
    mids = push_cfg.setdefault("mids", [])
    mid = _parse_mid(parts[index]) if len(parts) > index else 0
    if action == "add":
        if not mid:
            await d._reply(group_id, user_id, usage)
            return
        if mid in mids:
            await d._reply(group_id, user_id, "这个UP主已经在盯了")
            return
        mids.append(mid)
        _commit(d, cfg)
        # Prime seen-list + watermark so historical uploads never flood
        from ..bilibili import prime_push_state
        nickname = ""
        try:
            videos = await prime_push_state(d, target_group, mid)
            if videos:
                nickname = videos[0].get("author", "")
        except Exception as e:
            log.warning("bili push prime failed mid=%s: %s", mid, e)
        who = "UP主 {}（mid={}）".format(nickname, mid) if nickname else "mid={}".format(mid)
        await d._reply(group_id, user_id,
                       "好，开始盯 {} 的新投稿和动态，已有的不会重复推\n"
                       "小提示：动态推送用的是提供cookie的那个B站账号的关注列表，"
                       "记得让它也关注这个UP主，不然只有视频投稿推送".format(who))
    elif action == "del":
        if not mid:
            await d._reply(group_id, user_id, usage)
            return
        if mid in mids:
            mids.remove(mid)
            _commit(d, cfg)
            await d._reply(group_id, user_id, "不盯 mid={} 了".format(mid))
        else:
            await d._reply(group_id, user_id, "这个UP主本来就没在盯")
    elif action == "list":
        if mids:
            await d._reply(group_id, user_id,
                           "本群正在盯的UP主 mid：" + ", ".join(str(m) for m in mids)
                           + "\n（mid 就是 space.bilibili.com/ 后面的数字）")
        else:
            await d._reply(group_id, user_id,
                           "本群还没盯任何UP主\n" + usage)
    else:
        await d._reply(group_id, user_id, usage)

async def cmd_group_info(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个命令只能在群里用")
        return
    result = await d.client.get_group_info(group_id)
    data = result.get("data") or {}
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "群信息读取失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
        return
    lines = ["群名：{}".format(data.get("group_name", "未知")),
             "群号：{}".format(data.get("group_id", group_id)),
             "成员数：{}".format(data.get("member_count", "未知")),
             "上限：{}".format(data.get("max_member_count", "未知"))]
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_member_info(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个命令只能在群里用")
        return
    target = args.strip()
    if not target.isdigit():
        target = str(user_id)
    result = await d.client.get_group_member_info(group_id, int(target))
    data = result.get("data") or {}
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "成员信息读取失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
        return
    lines = ["QQ：{}".format(data.get("user_id", target)),
             "昵称：{}".format(data.get("nickname", "未知")),
             "名片：{}".format(data.get("card", "")),
             "角色：{}".format(data.get("role", "member")),
             "入群时间：{}".format(data.get("join_time", "未知"))]
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_member_list(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个命令只能在群里用")
        return
    result = await d.client.get_group_member_list(group_id)
    members = result.get("data") or []
    if result.get("status") != "ok" or not isinstance(members, list):
        await d._reply(group_id, user_id, "成员列表读取失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
        return
    keyword = args.strip().lower()
    if keyword:
        members = [m for m in members if keyword in str(m.get("nickname", "")).lower()
                   or keyword in str(m.get("card", "")).lower() or keyword == str(m.get("user_id", ""))]
    lines = ["群成员（最多显示20人）"]
    for item in members[:20]:
        name = item.get("card") or item.get("nickname") or "未知"
        lines.append("{}  {}  {}".format(item.get("user_id", ""), name, item.get("role", "member")))
    await d._reply(group_id, user_id, "\n".join(lines) if len(lines) > 1 else "没有匹配的成员")

async def cmd_file_system_info(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个命令只能在群里用")
        return
    result = await d.client.get_group_file_system_info(group_id)
    data = result.get("data") or {}
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "群文件状态读取失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
        return
    lines = ["群文件状态",
             "文件数：{} / {}".format(data.get("file_count", "未知"), data.get("limit_count", "未知")),
             "已用空间：{}".format(data.get("used_space", "未知")),
             "总空间：{}".format(data.get("total_space", "未知"))]
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_stranger_info(d, group_id, user_id, args, role, sender_card, message):
    target = args.strip()
    if not target.isdigit():
        await d._reply(group_id, user_id, "用法：/陌生人信息 QQ号")
        return
    result = await d.client.get_stranger_info(int(target))
    data = result.get("data") or {}
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "资料读取失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
        return
    lines = ["QQ：{}".format(data.get("user_id", target)),
             "昵称：{}".format(data.get("nickname", "未知")),
             "性别：{}".format(data.get("sex", "未知")),
             "年龄：{}".format(data.get("age", "未知"))]
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_friend_list(d, group_id, user_id, args, role, sender_card, message):
    result = await d.client.get_friend_list()
    friends = result.get("data") or []
    if result.get("status") != "ok" or not isinstance(friends, list):
        await d._reply(group_id, user_id, "好友列表读取失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
        return
    keyword = args.strip().lower()
    if keyword:
        friends = [f for f in friends if keyword in str(f.get("nickname", "")).lower()
                   or keyword in str(f.get("remark", "")).lower() or keyword == str(f.get("user_id", ""))]
    lines = ["好友列表（最多30人）"]
    for item in friends[:30]:
        lines.append("{}  {}{}".format(item.get("user_id", ""), item.get("nickname", "未知"),
                     "（{}）".format(item.get("remark")) if item.get("remark") else ""))
    await d._reply(group_id, user_id, "\n".join(lines) if len(lines) > 1 else "没有匹配好友")

async def cmd_delete_group_file(d, group_id, user_id, args, role, sender_card, message):
    parts = args.split()
    if not group_id or len(parts) < 2:
        await d._reply(group_id, user_id, "用法：/删除文件 file_id busid")
        return
    result = await d.client.delete_group_file(group_id, parts[0], parts[1])
    log.warning("ADMIN_ACTION actor=%s group=%s action=delete_file file=%s status=%s",
                user_id, group_id, parts[0], result.get("status"))
    await d._reply(group_id, user_id, "文件已删除" if result.get("status") == "ok" else
                   "删除失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_create_group_folder(d, group_id, user_id, args, role, sender_card, message):
    name = args.strip()[:120]
    if not group_id or not name:
        await d._reply(group_id, user_id, "用法：/新建文件夹 名称")
        return
    result = await d.client.create_group_file_folder(group_id, name)
    await d._reply(group_id, user_id, "文件夹已创建" if result.get("status") == "ok" else
                   "创建失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_delete_group_folder(d, group_id, user_id, args, role, sender_card, message):
    folder_id = args.strip()
    if not group_id or not folder_id:
        await d._reply(group_id, user_id, "用法：/删除文件夹 folder_id")
        return
    result = await d.client.delete_group_folder(group_id, folder_id)
    log.warning("ADMIN_ACTION actor=%s group=%s action=delete_folder folder=%s status=%s",
                user_id, group_id, folder_id, result.get("status"))
    await d._reply(group_id, user_id, "文件夹已删除" if result.get("status") == "ok" else
                   "删除失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_move_group_file(d, group_id, user_id, args, role, sender_card, message):
    parts = args.split()
    if not group_id or len(parts) < 3:
        await d._reply(group_id, user_id, "用法：/移动文件 file_id 当前目录 目标目录")
        return
    result = await d.client.move_group_file(group_id, parts[0], parts[1], parts[2])
    await d._reply(group_id, user_id, "文件已移动" if result.get("status") == "ok" else
                   "移动失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_rename_group_file(d, group_id, user_id, args, role, sender_card, message):
    parts = args.split(maxsplit=2)
    if not group_id or len(parts) < 3:
        await d._reply(group_id, user_id, "用法：/重命名文件 file_id 当前目录 新名称")
        return
    result = await d.client.rename_group_file(group_id, parts[0], parts[1], parts[2])
    await d._reply(group_id, user_id, "文件已重命名" if result.get("status") == "ok" else
                   "重命名失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_delete_group_notice(d, group_id, user_id, args, role, sender_card, message):
    notice_id = args.strip()
    if not group_id or not notice_id:
        await d._reply(group_id, user_id, "用法：/删公告 notice_id")
        return
    result = await d.client.del_group_notice(group_id, notice_id)
    log.warning("ADMIN_ACTION actor=%s group=%s action=delete_notice notice=%s status=%s",
                user_id, group_id, notice_id, result.get("status"))
    await d._reply(group_id, user_id, "公告已删除" if result.get("status") == "ok" else
                   "删除失败：" + str(result.get("msg") or result.get("wording") or result)[:180])

async def cmd_admin_mgr(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    parts = args.strip().split()
    action = parts[0].lower() if parts else ""
    mentions = d._extract_mentions(message)
    if not mentions:
        await d._reply(group_id, user_id, "请 @要操作的人")
        return
    target = mentions[0]
    if action == "add":
        r = await d.client.set_group_admin(group_id, target, True)
        if r.get("status") == "ok":
            await d._reply(group_id, user_id, "设好了：" + str(target))
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没设上，原因是：" + str(err))
    elif action == "del":
        r = await d.client.set_group_admin(group_id, target, False)
        if r.get("status") == "ok":
            await d._reply(group_id, user_id, "撤掉了：" + str(target))
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没撤掉，原因是：" + str(err))
    else:
        await d._reply(group_id, user_id, "这样用：/admin add @某人，或者 /admin del @某人")

async def cmd_special_title(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    caller_level, _ = await get_user_level(d, group_id, user_id, role)
    if user_id != d.config.get("bot_owner") and caller_level < LEVEL_MASTER:
        await d._reply(group_id, user_id, "这个只给最高主人或群主人用")
        return
    mentions = d._extract_mentions(message)
    clean_args = re.sub(r"\[CQ:[^]]+\]", "", args).strip()
    if not mentions:
        ids = re.findall(r"\b\d{5,12}\b", clean_args)
        mentions = [int(ids[0])] if ids else []
        if ids:
            clean_args = clean_args.replace(ids[0], "", 1).strip()
    if not mentions:
        await d._reply(group_id, user_id, "请 @要设置头衔的人")
        return
    title = clean_args.strip()
    if len(title) > 18:
        await d._reply(group_id, user_id, "头衔太长了，最多18个字左右")
        return
    target = mentions[0]
    result = await d.client.set_group_special_title(group_id, target, title)
    log.info("set_group_special_title completed: status=%s", result.get("status"))
    if result.get("status") != "ok" and title:
        # A timeout may still have applied server-side; verify before failing.
        try:
            info = await d.client.get_group_member_info(group_id, target)
            if (info.get("data") or {}).get("title", "") == title:
                result = {"status": "ok"}
        except Exception as error:
            log.debug("Special title verification failed: %s", error)
    if result.get("status") == "ok":
        from ..notice_handler import mark_title_set_by_bot
        mark_title_set_by_bot(group_id, target, title)
        await d._reply(group_id, user_id, "头衔设好了" if title else "头衔清掉了")
    else:
        await d._reply(group_id, user_id, "没设成：" + str(result.get("msg") or result.get("wording") or result)[:200])

async def cmd_my_title(d, group_id, user_id, args, role, sender_card, message):
    """我要头衔xxx — set the sender's special title. Only works when the bot is
    the group owner; otherwise the feature is ignored completely (silent)."""
    if not group_id:
        return
    bot_role_str, _ = await get_bot_role(d, group_id)
    if bot_role_str != "owner":
        return
    title = re.sub(r"\[CQ:[^\]]+\]", "", args or "").strip()
    if not title:
        return
    if len(title) > 18:
        await d._reply(group_id, user_id, "头衔太长了，18个字以内")
        return
    from event_policy import allow_event
    if not allow_event("mytitle", f"{group_id}_{user_id}", 60):
        await d._reply(group_id, user_id, "换太勤了，歇会再来")
        return
    result = await d.client.set_group_special_title(group_id, user_id, title)
    log.info("Self-title request completed: group=%s user=%s status=%s",
             group_id, user_id, result.get("status"))
    if result.get("status") != "ok":
        # A timeout may still have applied server-side; verify before failing.
        try:
            info = await d.client.get_group_member_info(group_id, user_id)
            if (info.get("data") or {}).get("title", "") == title:
                result = {"status": "ok"}
        except Exception as error:
            log.debug("Self-title verification failed: %s", error)
    if result.get("status") == "ok":
        from ..notice_handler import mark_title_set_by_bot
        mark_title_set_by_bot(group_id, user_id, title)
        await d._reply(group_id, user_id, "搞定，你的头衔现在是「" + title + "」了")
    else:
        err = str(result.get("msg") or result.get("wording") or "未知原因")[:120]
        await d._reply(group_id, user_id, "没设成：" + err)

async def cmd_group_ai_switch(d, group_id, user_id, args, role, sender_card, message):
    """/AI聊天 on|off — toggle AI chat for this group (owner/bot account only)."""
    await _toggle_group_feature(d, group_id, user_id, args,
                                "ai_chat", "AI聊天", "AI聊天")

async def cmd_private_ai_switch(d, group_id, user_id, args, role, sender_card, message):
    """/私聊AI on|off|allow QQ|deny QQ — global private-chat AI switch + allowlist."""
    parts = args.strip().split()
    action = parts[0].lower() if parts else "status"
    cfg = _load()
    pc = cfg.setdefault("private_chat", {"enabled": False, "allowed_users": []})
    pc.setdefault("enabled", False)
    allowed = pc.setdefault("allowed_users", [])
    if action == "on":
        pc["enabled"] = True
        _commit(d, cfg)
        await d._reply(group_id, user_id, "私聊AI已开启，所有好友都能聊了")
    elif action == "off":
        pc["enabled"] = False
        _commit(d, cfg)
        await d._reply(group_id, user_id, "私聊AI已关闭，只有开放名单里的人能聊")
    elif action == "allow" and len(parts) >= 2 and parts[1].isdigit():
        qq = int(parts[1])
        allowed = [int(u) for u in allowed if str(u).isdigit()]
        if qq not in allowed:
            allowed.append(qq)
        pc["allowed_users"] = allowed[-50:]
        _commit(d, cfg)
        await d._reply(group_id, user_id, "已开放私聊AI：" + str(qq))
    elif action == "deny" and len(parts) >= 2 and parts[1].isdigit():
        qq = int(parts[1])
        pc["allowed_users"] = [int(u) for u in allowed if str(u).isdigit() and int(u) != qq]
        _commit(d, cfg)
        await d._reply(group_id, user_id, "已移出开放名单：" + str(qq))
    else:
        status_text = "开启" if pc.get("enabled") else "关闭"
        users = ", ".join(str(u) for u in pc.get("allowed_users", [])) or "无"
        await d._reply(group_id, user_id,
                       "私聊AI：" + status_text + "\n开放名单：" + users +
                       "\n用法：/私聊AI on|off|allow QQ|deny QQ")

async def cmd_master(d, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split()
    action = parts[0].lower() if parts else "list"
    target_group = group_id
    target_index = 1
    if not group_id:
        if len(parts) >= 2 and parts[1].isdigit():
            target_group = int(parts[1])
            target_index = 2
        elif len(parts) >= 1 and parts[0] == "list":
            target_group = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            target_index = 2
        else:
            await d._reply(None, user_id, "私聊这样用：/master add 群号 QQ，或者 /master list 群号")
            return
    if not target_group:
        await d._reply(None, user_id, "要带上群号，不然我不知道改哪个群")
        return
    target_qq = int(parts[target_index]) if len(parts) > target_index and parts[target_index].isdigit() else 0
    if action == "add" and target_qq:
        if add_master(d, target_group, target_qq):
            await d._reply(group_id, user_id, "加好了，群 " + str(target_group) + " 的主人多了一个：" + str(target_qq))
        else:
            await d._reply(group_id, user_id, "这个人已经是主人了")
    elif action == "del" and target_qq:
        if remove_master(d, target_group, target_qq):
            await d._reply(group_id, user_id, "删掉了，群 " + str(target_group) + " 的主人移除了：" + str(target_qq))
        else:
            await d._reply(group_id, user_id, "这个人本来就不是主人")
    elif action == "list":
        masters = list_masters(d, target_group)
        if masters:
            await d._reply(group_id, user_id, "群 " + str(target_group) + " 当前主人：" + ", ".join(str(m) for m in masters))
        else:
            await d._reply(group_id, user_id, "这个群还没设置主人")
    else:
        await d._reply(group_id, user_id, "用法：/master add QQ，/master del QQ，/master list")

async def cmd_welcome(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    cfg = _load()
    arg = args.strip()
    gid = str(group_id)
    groups = cfg.setdefault("groups", {})
    group_cfg = groups.setdefault(gid, {"enabled": True, "masters": [], "welcome_msg": {}, "bad_words": {}, "features": {}})
    gcfg = group_cfg
    w = gcfg.setdefault("welcome_msg", {"enabled": True, "template": "欢迎 {nickname} 加入本群！"})
    if arg == "on":
        w["enabled"] = True
        _commit(d, cfg)
        await d._reply(group_id, user_id, "入群欢迎已开启")
    elif arg == "off":
        w["enabled"] = False
        _commit(d, cfg)
        await d._reply(group_id, user_id, "入群欢迎已关闭")
    elif arg:
        w["template"] = arg
        _commit(d, cfg)
        await d._reply(group_id, user_id, "欢迎语改好了：" + arg)
    else:
        status_text = "开启" if w["enabled"] else "关闭"
        await d._reply(group_id, user_id,
                       "入群欢迎状态: " + status_text + "\n当前模板: " + w.get("template", ""))

async def cmd_enable(d, group_id, user_id, args, role, sender_card, message):
    target_groups, error = resolve_scoped_group_targets(
        d, group_id, user_id, args, allow_all=True)
    if error:
        await d._reply(group_id, user_id, error)
        return
    cfg = _load()
    groups = cfg.setdefault("groups", {})
    enabled_list = []
    for gid in target_groups:
        if gid not in groups:
            groups[gid] = {
                "enabled": True, "masters": [],
                "welcome_msg": cfg.get("group_defaults", {}).get("welcome_msg", {"enabled": True, "template": "欢迎 {nickname}！"}),
                "bad_words": cfg.get("group_defaults", {}).get("bad_words", {"enabled": True, "auto_delete": True, "warn_msg": "@{user} 请注意文明发言！", "words": []}),
                "features": dict(cfg.get("group_defaults", {}).get("features", {})),
            }
        groups[gid]["enabled"] = True
        enabled_list.append(gid)
    _commit(d, cfg)
    msg = f"已启用 {len(enabled_list)} 个群"
    if len(enabled_list) <= 5:
        msg += f": {', '.join(enabled_list)}"
    await d._reply(group_id, user_id, msg + "，我来了")

async def cmd_disable(d, group_id, user_id, args, role, sender_card, message):
    target_groups, error = resolve_scoped_group_targets(
        d, group_id, user_id, args, allow_all=True, require_configured=True)
    if error:
        await d._reply(group_id, user_id, error)
        return
    cfg = _load()
    groups = cfg.setdefault("groups", {})
    disabled_list = []
    for gid in target_groups:
        if gid in groups:
            groups[gid]["enabled"] = False
            disabled_list.append(gid)
    if disabled_list:
        _commit(d, cfg)
        cleanup = getattr(d, "_cleanup_stale_state", None)
        if cleanup:
            cleanup()
        msg = f"已关闭 {len(disabled_list)} 个群"
        if len(disabled_list) <= 5:
            msg += f": {', '.join(disabled_list)}"
        await d._reply(group_id, user_id, msg + "，我先潜了")
    else:
        await d._reply(group_id, user_id, "没找到能关闭的群")

async def cmd_group(d, group_id, user_id, args, role, sender_card, message):
    """/group enable|disable 群号... — owner alias matching the documented form."""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    if sub == "enable":
        await cmd_enable(d, group_id, user_id, rest, role, sender_card, message)
    elif sub == "disable":
        await cmd_disable(d, group_id, user_id, rest, role, sender_card, message)
    else:
        await d._reply(group_id, user_id,
                       "用法：/group enable 群号 [群号...] 或 /group disable 群号 [群号...]\n"
                       "多个群号用空格分开，all 表示全部已配置群")
