"""Help, diagnostics, security, history, and lifecycle commands."""

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
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

async def cmd_api_status(d, group_id, user_id, args, role, sender_card, message):
    """Show the registered API catalog without probing every endpoint."""
    from api_registry import get_api_specs
    parts = args.strip().lower().split()
    category = parts[0] if parts and parts[0] not in ("summary", "状态") else None
    specs = get_api_specs(category)
    current = await d.client.api_status()
    statuses = {item["name"]: item["status"] for item in current}
    counts = {}
    for value in statuses.values():
        counts[value] = counts.get(value, 0) + 1
    lines = ["NapCat API 能力状态",
             "已确认支持={} 不支持={} 临时失败={} 未探测={}".format(
                 counts.get("supported", 0), counts.get("unsupported", 0),
                 counts.get("temporary_failed", 0), counts.get("unknown", 0))]
    for spec in specs:
        lines.append("{} [{}] risk={} AI={} status={}".format(
            spec.name, spec.category, spec.risk,
            "yes" if spec.ai_allowed else "no", statuses.get(spec.name, "unknown")))
    await d._reply(group_id, user_id, "\n".join(lines[:60]))

COMMAND_DETAILS = {
    "天气": "/天气 <城市>\n查真实天气（数据来自高德）。\n例：/天气 杭州",
    "热榜": "/热榜 [平台]（别名 /热搜）\n看指定平台的实时热榜前 10：AI 先用一两句话概括热点趋势，每条带可点击的详情链接。\n平台可写：微博、知乎、B站、抖音、百度、头条、IT之家、V2EX、GitHub、36氪、豆瓣电影\n例：/热榜 微博（不写平台默认微博）",
    "一言": "/一言\n随机来一句语录。",
    "答案之书": "/答案之书 [问题]\n心里想着问题，翻翻答案之书。\n例：/答案之书 今天适合摸鱼吗",
    "每日新闻": "/每日新闻\n发一张今日新闻速览图。",
    "必应壁纸": "/必应壁纸\n发今天的必应壁纸。",
    "epic免费": "/epic免费\n看 Epic 现在在送什么游戏。",
    "全体": "/全体 <内容>\n@全体成员发一条消息（每天次数有限，QQ 限制）。\n例：/全体 今晚八点开黑\n需要：你是本群管理/群主/主人，且 Bot 是管理或群主。",
    "acg图": "/acg图 on 或 /acg图 off\n开关本群的每日 ACG 图推送（每天在 4 个时间窗口内随机发送，每次严格攒够 20 张，图片 7 天内不重复）。\n不写参数可查看当前状态。管理员及以上可用。",
    "热榜推送": "/热榜推送 on 或 /热榜推送 off\n开关本群的每日热榜推送（每天上午和晚上各随机发送一次）。\n不写参数可查看当前状态。管理员及以上可用。",
    "b站解析": "/b站解析 on 或 /b站解析 off\n开关本群的 B站视频自动解析：有人发 BV号/av号/b23 链接时，自动回复视频信息并尽量发出视频本体。\n不写参数可查看当前状态。管理员及以上可用。",
    "b站推送": "/b站推送 add <UP主的mid> — 盯一个 UP 主，新投稿和新动态约 1 分钟内推到本群\n/b站推送 del <mid> — 不盯了\n/b站推送 list — 看本群在盯谁\n\nmid 是什么：UP 主空间网址 space.bilibili.com/ 后面的那串数字，直接贴空间链接也行。\n例：/b站推送 add 946974 或 /b站推送 add space.bilibili.com/946974\n视频投稿推送直接生效；动态推送需要提供cookie的B站账号也关注了这个UP主。\n只有群主人和总主人能用。",
    "积分": "/积分\n看 UApiS 官方剩余额度，以及 Bot 内部的命令/自动任务保护额度。",
    "master": "/master add <QQ号> — 添加本群主人\n/master del <QQ号> — 移除\n/master list — 看本群主人名单\n私聊里用：/master add <群号> <QQ号>\n群主人拥有本群全部管理权限。只有总主人和机器人账号能用。",
    "私聊ai": "/私聊AI on — 所有好友都能私聊 AI\n/私聊AI off — 全部关闭\n/私聊AI allow <QQ号> — 只给这个人开\n/私聊AI deny <QQ号> — 把这个人移出名单\n主人永远可用，不受开关影响。",
    "ai聊天": "/AI聊天 on 或 /AI聊天 off\n开关本群的 AI 聊天（@小汐 说话她回不回答）。\n私聊里跨群用：/AI聊天 <群号> on",
    "kick": "/kick @某人 — 踢出群\n也可以直接说：踢了 @某人\n需要管理权限，且不能踢同级及以上的人。",
    "ban": "/ban @某人 [分钟] — 禁言（默认 30 分钟）\n/unban @某人 — 解除禁言\n也可以说：禁言 @某人 10分钟",
    "title": "/title @某人 <头衔> — 设置专属头衔\nQQ 规定只有群主能设头衔，所以 Bot 必须是群主；Bot 不是群主的群此功能静默无效。\n任何人也可以发：我要头衔xxx（只给自己设，Bot 是群主才生效）。",
    "公告": "/公告 <内容> — 发布群公告\n/公告 — 查看现有公告\n/删公告 <notice_id> — 删除",
    "fortune": "/fortune\n今日运势，每人每天结果固定。",
    "rank": "/rank\n近期发言排行前 10。",
    "like": "/like [@人]\n给 TA 点 10 个赞，每天每人一次。",
    "calc": "/calc <算式>\n计算器。例：/calc 3*(4+5)",
    "translate": "/translate <文本>\n英译中。例：/translate hello world",
    "生图": "/生图 <描述>\nAI 画一张图。例：/生图 夕阳下的猫",
    "info": "/info [@人]\n看群成员资料，不写人名看自己。",
    "history": "/history [条数]\n看本群最近消息记录，默认 10 条。",
    "ocr": "/ocr\n回复一张图片，识别图上的文字。",
    "转发摘要": "/转发摘要\n回复一条合并转发消息，AI 帮你总结内容。",
    "群文件": "/群文件 [关键词]\n搜索或列出群文件。",
    "精华列表": "/精华列表\n看本群精华消息。",
    "群荣誉": "/群荣誉\n看本群群荣誉（龙王、话痨等）。",
    "禁言列表": "/禁言列表\n看本群正在被禁言的人，需要 Bot 是管理。",
    "点赞信息": "/点赞信息 [@人]\n看 TA 资料卡的点赞情况。",
    "health": "/health\n看 Bot 运行状态：内存、连接、AI 供应商健康度。",
    "gal": "/gal 作品名\n查询 TouchGal 的 Galgame 条目、平台和官方资源详情页。不会返回网盘直链。",
    "galgame": "/galgame 作品名\n查询 TouchGal 的 Galgame 条目、平台和官方资源详情页。",
    "游戏资源": "/游戏资源 作品名\n查询 TouchGal 的 Galgame 条目、平台和官方资源详情页。",
    "gal资源": "/gal资源 on/off\n开关本群 Galgame 资源自动回复。需要管理权限。",
    "allban": "/allban on 或 /allban off\n全员禁言开关。需要管理权限，且 Bot 是管理或群主。",
    "精华": "回复某条消息发 /精华\n把那条消息设为精华，再设一次取消。需要管理权限。",
    "welcome": "/welcome on 或 /welcome off — 开关入群欢迎\n/welcome 内容 — 自定义欢迎语，{nickname} 代表新人\n需要管理权限。",
    "badword": "/badword add <词> — 加违禁词\n/badword del <词> — 移除\n/badword list — 看列表\n有人发违禁词会自动撤回并提醒。需要管理权限。",
    "安全": "/安全 status — 安全功能状态\n/安全 log — 最近拦截记录\n群里的链接会自动检测安全性。需要管理权限。",
    "enable": "/enable — 在本群启用 Bot\n/disable — 关停本群\n群主人及以上可用。",
    "list": "/list\n本群数据概览（发言、AI、开关状态）。群主人及以上可用。",
    "clearai": "/clearai\n清空本群的 AI 记忆数据。群主人及以上可用。",
    "approve": "/approve — 通过待处理的加群/好友申请\n/reject — 拒绝\n总主人私聊使用。",
}

async def cmd_help(d, group_id, user_id, args, role, sender_card, message):
    # /help <命令名> — detailed usage for one command
    query = args.strip().lstrip("/").lower()
    if query:
        matched = None
        for name in d.commands:
            if name.lower() == query:
                matched = name
                break
        if not matched:
            for name in d.commands:
                if query in name.lower():
                    matched = name
                    break
        if not matched:
            await d._reply(group_id, user_id,
                           "没有这个命令，发 /help 看全部命令")
            return
        info = d.commands.get(matched, {})
        lines = ["【/{}】".format(matched)]
        detail = COMMAND_DETAILS.get(matched) or COMMAND_DETAILS.get(query)
        if detail:
            lines.append(detail)
        elif info.get("help"):
            lines.append(info["help"])
        else:
            lines.append("暂无详细说明")
        await d._reply(group_id, user_id, "\n".join(lines))
        return

    caller_level, caller_name = await get_user_level(d, group_id, user_id, role)
    bot_owner = d.config.get("bot_owner")
    is_super = (user_id == bot_owner) or (user_id == d.config.get("bot_qq"))
    bot_role_str = "member"
    if group_id:
        bot_role_str, _ = await get_bot_role(d, group_id)
    lines = []
    lines.append("* ====== 小汐的使用指南 ====== *")
    # ---- everyone ----
    lines.append("")
    lines.append("【聊天】")
    lines.append("  @小汐 + 想说的话      跟我聊天")
    lines.append("  点歌+歌名             搜歌分享")
    lines.append("  发B站链接/BV号        自动解析并发视频")
    lines.append("  我要头衔xxx           给自己设专属头衔(Bot是群主才生效)")
    lines.append("")
    lines.append("【娱乐查询】")
    lines.append("  /天气 <城市>    /热榜 [平台]    /一言")
    lines.append("  /答案之书 [问题]      /epic免费")
    lines.append("  /每日新闻       /必应壁纸")
    lines.append("  /fortune 今日运势     /rank 发言排行")
    lines.append("  /like 赞我            /calc 计算器")
    lines.append("  /translate 翻译       /生图 AI画图")
    lines.append("")
    lines.append("【查询工具】")
    lines.append("  /info [@人]           成员资料")
    lines.append("  /history [条数]       最近消息")
    lines.append("  /ocr                  识别图片文字")
    lines.append("  /转发摘要             总结合并转发")
    lines.append("  /群文件 [关键词]      搜群文件")
    lines.append("  /精华列表 /群荣誉     群内容")
    lines.append("  /禁言列表 /点赞信息   其他查询")
    lines.append("  /health               运行状态")
    lines.append("  /gal <作品名>         查 TouchGal Galgame 详情页")
    # ---- admin tier (QQ admin/group owner/master/super) ----
    if caller_level >= LEVEL_ADMIN and bot_role_str in ("admin", "owner"):
        lines.append("")
        lines.append("【管理命令】(你是管理，可用)")
        lines.append("  /kick @人             踢出群")
        lines.append("  /ban @人 [分钟]       禁言(默认30)")
        lines.append("  /unban @人            解除禁言")
        lines.append("  /allban on/off        全员禁言")
        lines.append("  /全体 <内容>          @全体成员")
        lines.append("  /公告 <内容>          发群公告")
        lines.append("  /精华                 回复消息设精华")
        lines.append("  /welcome on/off/内容  入群欢迎")
        lines.append("  /badword add/del/list 违禁词")
        lines.append("  /acg图 on/off         每日ACG图开关")
        lines.append("  /热榜推送 on/off      每日热榜开关")
        lines.append("  /b站解析 on/off       B站解析开关")
        lines.append("  /gal资源 on/off       Galgame资源自动回复开关")
        lines.append("  /安全 status/log      安全功能")
        if bot_role_str == "owner":
            lines.append("  /title @人 头衔       设专属头衔")
    elif caller_level >= LEVEL_ADMIN:
        lines.append("")
        lines.append("（你有管理身份，但我现在不是本群管理，管理命令暂时都用不了）")
    # ---- master tier ----
    if caller_level >= LEVEL_MASTER:
        lines.append("")
        lines.append("【群主人命令】(你是本群主人，可用)")
        lines.append("  /enable /disable      开/关本群功能")
        lines.append("  /list                 群数据概览")
        lines.append("  /clearai              清本群AI数据")
        lines.append("  /b站推送 add/del/list 盯UP主新投稿")
        lines.append("  /积分                 uapis额度用量")
    # ---- super tier ----
    if is_super:
        lines.append("")
        lines.append("【最高主人命令】")
        lines.append("  /master add/del/list  设置群主人")
        lines.append("  /私聊AI on/off/allow/deny 私聊AI开关")
        lines.append("  /AI聊天 on/off        本群AI聊天开关")
        lines.append("  /approve /reject      处理加群/好友申请")
        lines.append("  私聊跨群：/命令 群号 参数")
    lines.append("")
    lines.append("发 /help <命令名> 看某个命令的详细用法")
    lines.append("比如 /help b站推送")
    lines.append("* ============================ *")
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_health(d, group_id, user_id, args, role, sender_card, message):
    import subprocess
    lines = []
    try:
        bot_state = subprocess.run(["systemctl", "is-active", "qqbot.service"],
                                   capture_output=True, text=True, timeout=3)
        napcat_state = subprocess.run(["systemctl", "is-active", "napcat.service"],
                                      capture_output=True, text=True, timeout=3)
        lines.append("小汐: " + (bot_state.stdout.strip() or "unknown"))
        lines.append("NapCat: " + (napcat_state.stdout.strip() or "unknown"))
    except Exception as e:
        lines.append("服务状态读取失败: " + str(e))
    try:
        meminfo = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, value = line.split(":", 1)
                meminfo[key] = int(value.strip().split()[0])
        total = meminfo.get("MemTotal", 0) // 1024
        available = meminfo.get("MemAvailable", 0) // 1024
        swap_total = meminfo.get("SwapTotal", 0) // 1024
        swap_free = meminfo.get("SwapFree", 0) // 1024
        lines.append("内存: 可用{}M/总{}M".format(available, total))
        lines.append("Swap: 可用{}M/总{}M".format(swap_free, swap_total))
    except Exception:
        lines.append("内存: unknown")
    lines.append("WS: " + ("connected" if d.client._ws is not None else "disconnected"))
    lines.append("事件任务: {}".format(len(getattr(d.client, "_event_tasks", []))))
    lines.append("后台任务: {}".format(len(getattr(d, "_background_tasks", []))))
    if group_id:
        bot_role, _ = await get_bot_role(d, group_id)
        gcfg = get_group_config(d, group_id)
        lines.append("本群: {} bot身份: {}".format("开启" if gcfg.get("enabled") else "关闭", bot_role))
    if user_id == d.config.get("bot_owner"):
        try:
            from ..request_handler import load_pending_requests
            lines.append("待处理申请: {}".format(len(load_pending_requests())))
        except Exception:
            pass
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_security(d, group_id, user_id, args, role, sender_card, message):
    from ..security import security_config
    sec = security_config(d, group_id)
    sub = args.strip().lower()
    if not sub or sub in ("status", "状态"):
        lines = [
            "安全功能" + ("（本群）" if group_id else "（全局）"),
            "URL检测: " + ("开" if sec.get("url_check_enabled", True) else "关"),
            "灰条保护: " + ("开" if sec.get("gray_tip_protect_enabled", True) else "关"),
            "自动处罚: " + ("开" if sec.get("auto_punish", True) else "关"),
            "禁言秒数: " + str(sec.get("ban_seconds", 600)),
        ]
        await d._reply(group_id, user_id, "\n".join(lines))
        return
    if sub.startswith("log") or sub.startswith("日志"):
        from ..security import format_security_events
        parts = sub.split()
        limit = 10
        if len(parts) >= 2:
            try:
                limit = int(parts[1])
            except Exception:
                pass
        await d._reply(group_id, user_id, format_security_events(group_id=group_id, limit=limit))
        return
    parts = sub.split()
    if len(parts) >= 2 and parts[0] in ("url", "gray", "灰条", "punish", "处罚"):
        enabled = parts[1] in ("on", "开", "enable", "enabled", "true", "1")
        if parts[0] == "url":
            key = "url_check_enabled"
            name = "URL检测"
        elif parts[0] in ("gray", "灰条"):
            key = "gray_tip_protect_enabled"
            name = "灰条保护"
        else:
            key = "auto_punish"
            name = "自动处罚"
        cfg = _load()
        if group_id:
            g = cfg.setdefault("groups", {}).setdefault(str(group_id), {})
            g.setdefault("security", {})[key] = enabled
        else:
            cfg.setdefault("security", {})[key] = enabled
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "{}已{}".format(name, "开启" if enabled else "关闭"))
        return
    if len(parts) >= 2 and parts[0] in ("ban", "禁言"):
        try:
            seconds = max(0, min(int(parts[1]), 86400))
        except Exception:
            await d._reply(group_id, user_id, "禁言秒数要写数字，比如 /安全 ban 600")
            return
        cfg = _load()
        if group_id:
            g = cfg.setdefault("groups", {}).setdefault(str(group_id), {})
            g.setdefault("security", {})["ban_seconds"] = seconds
        else:
            cfg.setdefault("security", {})["ban_seconds"] = seconds
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "安全禁言秒数已设为 " + str(seconds))
        return
    await d._reply(group_id, user_id, "用法：/安全 status | /安全 log | /安全 url on/off | /安全 gray on/off | /安全 punish on/off | /安全 ban 秒数")

async def cmd_clear_ai(d, group_id, user_id, args, role, sender_card, message):
    import glob as _glob2
    # Determine target groups
    target_groups = []
    if not group_id:
        # Private message: args required or clear all
        if args.strip():
            target_groups = [g.strip() for g in args.split() if g.strip().isdigit()]
        if not target_groups:
            # Clear ALL configured groups
            target_groups = list(d.config.get("groups", {}).keys())
    else:
        # Group message: args are optional extra groups, always include current
        target_groups = [str(group_id)]
        if args.strip():
            extra = [g.strip() for g in args.split() if g.strip().isdigit()]
            for g in extra:
                if g not in target_groups:
                    target_groups.append(g)
    cleared = []
    for gid in target_groups:
        # 1. Clear AI chat memory
        from ..ai import clear_group_memory
        clear_group_memory(d, gid)
        # 2. Clear stickers
        import os as _os3
        sticker_path = _os3.path.join(_os3.path.dirname(_os3.path.dirname(_os3.path.abspath(__file__))),
                                    "data", "stickers", f"group_{gid}.json")
        if _os3.path.exists(sticker_path):
            _os3.remove(sticker_path)
        # 3. Clear blacklist entries for this group
        from ..guard import load_blacklist, save_blacklist
        bl = load_blacklist()
        prefix = f"{gid}_"
        removed = [k for k in bl if k.startswith(prefix)]
        for k in removed:
            del bl[k]
        if removed:
            save_blacklist(bl)
        # 4. Clear R18 warnings for this group
        try:
            from ..guard import load_warnings, save_warnings
            w = load_warnings()
            removed_w = [k for k in w if k.startswith(prefix)]
            for k in removed_w:
                del w[k]
            if removed_w:
                save_warnings(w)
        except Exception:
            pass
        # 5. Clear user memories for this group
        user_mem_dir = _os3.path.join(_os3.path.dirname(_os3.path.dirname(_os3.path.abspath(__file__))),
                                    "data", "memories")
        pattern = _os3.path.join(user_mem_dir, f"group_{gid}_u*.json")
        for f in _glob2.glob(pattern):
            _os3.remove(f)
        cleared.append(gid)
    if not group_id:
        await d._reply(None, user_id, f"清完了，一共 {len(cleared)} 个群：{', '.join(cleared)}")
    else:
        msg = f"清完了，一共 {len(cleared)} 个群"
        if len(cleared) > 1:
            msg += f"：{', '.join(cleared)}"
        await d._reply(group_id, user_id, msg)

async def cmd_list(d, group_id, user_id, args, role, sender_card, message):
    import os as _os_list, glob as _glob_list, json as _json_list, time as _time_list

    cfg = d.config
    groups_cfg = cfg.get("groups", {})
    data_root = _os_list.path.join(_os_list.path.dirname(_os_list.path.dirname(_os_list.path.abspath(__file__))), "data")

    if not groups_cfg:
        await d._reply(group_id, user_id, "还没有配置群")
        return

    requested = [p for p in args.split() if p.isdigit()]
    if group_id:
        target_groups = {str(group_id): groups_cfg.get(str(group_id), {})}
    elif requested:
        target_groups = {gid: groups_cfg.get(gid, {}) for gid in requested}
    else:
        target_groups = groups_cfg
    def _json_count(path):
        if not _os_list.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                data = _json_list.load(f)
            if isinstance(data, (list, dict)):
                return len(data)
        except Exception:
            pass
        return 0
    def _size_kb(path):
        try:
            return max(1, _os_list.path.getsize(path) // 1024)
        except OSError:
            return 0
    bl_path = _os_list.path.join(data_root, "blacklist.json")
    rw_path = _os_list.path.join(data_root, "r18_warnings.json")
    try:
        with open(bl_path, encoding="utf-8") as f:
            bl_data = _json_list.load(f)
    except Exception:
        bl_data = {}
    try:
        with open(rw_path, encoding="utf-8") as f:
            rw_data = _json_list.load(f)
    except Exception:
        rw_data = {}
    lines = ["小汐当前群数据概览", ""]
    for gid, gcfg in sorted(target_groups.items()):
        mem_path = _os_list.path.join(data_root, "memories", "group_{}.json".format(gid))
        lmem_path = _os_list.path.join(data_root, "memories", "group_{}_long.json".format(gid))
        user_pattern = _os_list.path.join(data_root, "memories", "group_{}_u*.json".format(gid))
        st_path = _os_list.path.join(data_root, "stickers", "group_{}.json".format(gid))
        prefix = "{}_".format(gid)
        active_bl = sum(1 for k, v in bl_data.items() if k.startswith(prefix) and v.get("expires", 0) > _time_list.time())
        warning_users = sum(1 for k in rw_data if k.startswith(prefix))
        enabled = "开" if gcfg.get("enabled", False) else "关"
        masters = len(gcfg.get("masters", []) or [])
        user_files = _glob_list.glob(user_pattern)
        total_kb = sum(_size_kb(p) for p in [mem_path, lmem_path, st_path] if _os_list.path.exists(p))
        lines.append(
            "群 {gid}：{enabled}，主人 {masters} 个，群记忆 {mem} 条，长期记忆 {long} 条，"
            "个人记忆 {users} 份，表情 {stickers} 个，黑名单 {bl} 个，警告 {warn} 人，数据约 {kb} 千字节".format(
                gid=gid, enabled=enabled, masters=masters, mem=_json_count(mem_path),
                long=_json_count(lmem_path), users=len(user_files), stickers=_json_count(st_path),
                bl=active_bl, warn=warning_users, kb=total_kb,
            )
        )
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3400] + "\n\n内容太多，我先截到这里。要看单个群可以用 /list 群号"
    await d._reply(group_id, user_id, text)

async def cmd_history(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个只能在群里用")
        return
    count = 10
    if args.strip():
        try:
            count = max(1, min(int(args.strip()), 50))
        except ValueError:
            pass
    r = await d.client.get_group_msg_history(group_id, count)
    if r.get("status") != "ok":
        await d._reply(group_id, user_id, "获取历史消息失败：" + str(r.get("msg") or r.get("wording") or r)[:200])
        return
    data = r.get("data", {})
    messages = data.get("messages") if isinstance(data, dict) else []
    if not messages:
        await d._reply(group_id, user_id, "没拿到历史消息")
        return
    import re as _re_hist
    lines = [f"最近 {len(messages)} 条消息"]
    for msg in messages[-15:]:
        sender = msg.get("sender", {})
        name = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))
        raw_msg = msg.get("raw_message", "") or ""
        clean = _re_hist.sub(r"\[CQ:[^\]]+\]", "", raw_msg).strip()
        if clean:
            lines.append(f"  {name}: {clean[:60]}")
    text = "\n".join(lines)
    if len(text) > 2000:
        text = text[:1950] + "\n..."
    await d._reply(group_id, user_id, text)

async def cmd_shut_list(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        await d._reply(group_id, user_id, "这个只能在群里用")
        return
    r = await d.client.get_group_shut_list(group_id)
    if r.get("status") != "ok":
        await d._reply(group_id, user_id, "获取禁言列表失败：" + str(r.get("msg") or r.get("wording") or r)[:200])
        return
    shut_list = r.get("data", [])
    if not shut_list:
        await d._reply(group_id, user_id, "当前没有被禁言的人")
        return
    lines = [f"当前被禁言的人（{len(shut_list)} 人）"]
    for item in shut_list[:20]:
        qq = item.get("user_id", "")
        nick = item.get("nickname", "") or item.get("card", "") or str(qq)
        lines.append(f"  {nick}({qq})")
    if len(shut_list) > 20:
        lines.append(f"  ... 还有 {len(shut_list) - 20} 人")
    await d._reply(group_id, user_id, "\n".join(lines))

async def cmd_sysmsg(d, group_id, user_id, args, role, sender_card, message):
    from ..request_handler import format_pending_requests
    local_text = format_pending_requests(limit=10)
    r = await d.client.get_group_system_msg()
    if r.get("status") != "ok":
        await d._reply(
            group_id,
            user_id,
            local_text + "\n\nNapCat系统消息获取失败：" + str(r.get("msg") or r)[:200],
        )
        return
    data = r.get("data", {})
    invitate = data.get("invitate_messages", []) or []
    join = data.get("join_messages", []) or []
    lines = [local_text, ""]
    if invitate:
        lines.append(f"邀请消息（{len(invitate)} 条）")
        for item in invitate[:5]:
            inviter = item.get("inviter", {})
            invitee = item.get("invitee", {})
            group = item.get("group", {})
            lines.append(f"  {inviter.get('nickname', '')} 邀请 {invitee.get('nickname', '')} 加入 {group.get('group_name', '')}")
    if join:
        lines.append(f"入群消息（{len(join)} 条）")
        for item in join[:5]:
            user = item.get("user", {})
            group = item.get("group", {})
            lines.append(f"  {user.get('nickname', '')} 申请加入 {group.get('group_name', '')}")
    if not invitate and not join:
        await d._reply(group_id, user_id, local_text + "\n\nNapCat没有待处理系统消息")
    else:
        await d._reply(group_id, user_id, "\n".join(lines))
