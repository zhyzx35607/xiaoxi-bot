"""Help, diagnostics, security, history, and lifecycle commands."""

import asyncio
import glob
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
    save_group_config, can_moderate_target, LEVEL_SUPER, LEVEL_MASTER,
    LEVEL_GOWNER, LEVEL_ADMIN, LEVEL_MEMBER,
)
from ..events.context import _service_state
from ..services.confirmations import create_confirmation
from ..utils import atomic_write_json
from .common import CONFIG_PATH, _commit, _load, _save, resolve_scoped_group_targets
from .random_image import RANDOM_IMAGE_HELP

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_linux_memory():
    meminfo = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])
    return {
        "total": meminfo.get("MemTotal", 0) // 1024,
        "available": meminfo.get("MemAvailable", 0) // 1024,
        "swap_total": meminfo.get("SwapTotal", 0) // 1024,
        "swap_free": meminfo.get("SwapFree", 0) // 1024,
    }


def _read_json_container(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        log.warning("Runtime data read failed for %s: %s", os.path.basename(path), error)
        return default
    if not isinstance(value, type(default)):
        log.warning("Runtime data has unexpected root type: %s", os.path.basename(path))
        return default
    return value


def _json_count(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        log.warning("Runtime data count failed for %s: %s", os.path.basename(path), error)
        return 0
    return len(value) if isinstance(value, (list, dict)) else 0


def _size_kb(path):
    try:
        return max(1, os.path.getsize(path) // 1024)
    except OSError:
        return 0


def _build_group_data_overview(data_root, target_groups):
    bl_path = os.path.join(data_root, "blacklist.json")
    rw_path = os.path.join(data_root, "r18_warnings.json")
    bl_data = _read_json_container(bl_path, {})
    rw_data = _read_json_container(rw_path, {})
    lines = ["小汐当前群数据概览", ""]
    now = time.time()
    for gid, gcfg in sorted(target_groups.items()):
        mem_path = os.path.join(data_root, "memories", "group_{}.json".format(gid))
        lmem_path = os.path.join(data_root, "memories", "group_{}_long.json".format(gid))
        user_pattern = os.path.join(data_root, "memories", "group_{}_u*.json".format(gid))
        st_path = os.path.join(data_root, "stickers", "group_{}.json".format(gid))
        prefix = "{}_".format(gid)
        active_bl = sum(
            1 for key, value in bl_data.items()
            if key.startswith(prefix) and isinstance(value, dict) and value.get("expires", 0) > now
        )
        warning_users = sum(1 for key in rw_data if key.startswith(prefix))
        enabled = "开" if gcfg.get("enabled", False) else "关"
        masters = len(gcfg.get("masters", []) or [])
        user_files = glob.glob(user_pattern)
        total_kb = sum(_size_kb(path) for path in (mem_path, lmem_path, st_path))
        lines.append(
            "群 {gid}：{enabled}，主人 {masters} 个，群记忆 {mem} 条，长期记忆 {long} 条，"
            "个人记忆 {users} 份，表情 {stickers} 个，黑名单 {bl} 个，警告 {warn} 人，数据约 {kb} 千字节".format(
                gid=gid, enabled=enabled, masters=masters, mem=_json_count(mem_path),
                long=_json_count(lmem_path), users=len(user_files), stickers=_json_count(st_path),
                bl=active_bl, warn=warning_users, kb=total_kb,
            )
        )
    return lines

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
    "b站推送": "/b站推送 add <UP主的mid> — 盯一个 UP 主，新投稿和新动态约 1 分钟内推到本群\n/b站推送 del <mid> — 不盯了\n/b站推送 list — 看本群在盯谁\n/b站推送 atall on/off — 推送时是否 @全体成员（默认开；只有 Bot 是管理/群主时才会 @）\n\nmid 是什么：UP 主空间网址 space.bilibili.com/ 后面的那串数字，直接贴空间链接也行。\n例：/b站推送 add 946974 或 /b站推送 add space.bilibili.com/946974\n视频投稿推送直接生效；动态推送需要提供cookie的B站账号也关注了这个UP主。\n只有群主人和总主人能用。",
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
    "随机图": RANDOM_IMAGE_HELP,
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
    "group": "/group enable 群号 [群号...] — 启用群\n/group disable 群号 [群号...] — 关停群\n多个群号用空格分开，all 表示全部已配置群。最高主人私聊使用。",
    "list": "/list\n本群数据概览（发言、AI、开关状态）。群主人及以上可用。",
    "clearai": "/clearai\n清空本群的 AI 记忆数据。群主人及以上可用。",
    "approve": "/approve — 通过待处理的加群/好友申请\n/reject — 拒绝\n总主人私聊使用。",
}

_COMMAND_DETAIL_ALIASES = {
    "weather": "天气",
    "热搜": "热榜",
    "pixiv图": "随机图",
    "unban": "ban",
    "头衔": "title",
    "reject": "approve",
    "disable": "enable",
}

_HELP_CATEGORIES = {
    "聊天与互动": {"help", "like", "点赞信息", "戳", "表情回应", "已读", "转发", "转发摘要", "mytitle", "头衔"},
    "娱乐与查询": {"weather", "天气", "热榜", "热搜", "一言", "答案之书", "每日新闻", "必应壁纸", "epic免费", "fortune", "rank", "calc", "translate", "gal", "galgame", "游戏资源"},
    "图片与媒体": {"图片描述", "ocr", "生图", "随机图"},
    "群资料与成员": {"群信息", "成员", "成员列表", "陌生人信息", "info", "群荣誉", "禁言列表", "精华列表", "history"},
    "文件与内容": {"群文件", "文件状态", "文件链接", "删除文件", "新建文件夹", "删除文件夹", "移动文件", "重命名文件", "精华", "删精华", "公告", "删公告", "setgroupavatar"},
    "群管理": {"kick", "ban", "unban", "allban", "welcome", "badword", "安全", "全体", "title", "审批"},
    "功能与自动化": {"enable", "disable", "group", "list", "clearai", "ai聊天", "私聊ai", "acg图", "热榜推送", "b站解析", "b站推送", "gal资源", "积分"},
    "主人与维护": {"api", "health", "好友列表", "sysmsg", "approve", "reject", "master", "admin"},
}

_PRIVATE_VISIBLE = {
    "help", "like", "点赞信息", "陌生人信息", "weather", "天气", "热榜", "热搜", "一言",
    "答案之书", "每日新闻", "必应壁纸", "epic免费", "fortune", "calc", "translate", "ocr",
    "图片描述", "生图", "随机图", "pixiv图", "gal", "galgame", "游戏资源",
    "health", "积分", "私聊ai",
}


def _help_permission_label(info):
    if info.get("bot_owner_only"):
        return "最高主人"
    if info.get("owner_only"):
        return "群主或最高主人"
    if info.get("bot_owner"):
        return "群主人及以上"
    if info.get("admin_only"):
        return "管理员及以上"
    return "所有人"


def _help_visible(info, level, group_id):
    if info.get("bot_owner_only") and level < LEVEL_SUPER:
        return False
    if info.get("owner_only") and level < LEVEL_GOWNER:
        return False
    if info.get("bot_owner") and level < LEVEL_MASTER:
        return False
    if info.get("admin_only") and level < LEVEL_ADMIN:
        return False
    return True


def build_help_digest(commands, level, query="", *, group_id=0, bot_role="member"):
    """按调用者身份生成帮助文本，供 /help 与 AI 工具 get_bot_help 共用。

    返回 (status, name, text)：status 为 "ok" / "not_found" / "denied"。
    空 query 返回分类概览；命令名返回详细用法；分类名返回该分类的命令列表。
    """
    query = str(query or "").strip().lstrip("/").lower()

    def _visible_names(names):
        visible = []
        for name, info in commands.items():
            if name not in names:
                continue
            if not group_id and level < LEVEL_SUPER and name not in _PRIVATE_VISIBLE:
                continue
            if not _help_visible(info, level, group_id):
                continue
            visible.append(name)
        return visible

    if query:
        category = next(
            (name for name in _HELP_CATEGORIES if name.lower() == query), None)
        if category:
            names = _visible_names(_HELP_CATEGORIES[category])
            if not names:
                return "denied", category, ""
            return "ok", category, "【{}】\n{}".format(
                category, " ".join("/" + name for name in names))
        matched = next((name for name in commands if name.lower() == query), None)
        if not matched:
            matched = next((name for name in commands if query in name.lower()), None)
        if not matched:
            return "not_found", None, ""
        info = commands.get(matched, {})
        if not _help_visible(info, level, group_id):
            return "denied", matched, ""
        return "ok", matched, _help_command_text(matched, info, bot_role, group_id)

    lines = ["小汐功能概览（按你的身份过滤）"]
    assigned = set()
    for category, names in _HELP_CATEGORIES.items():
        visible = [name for name in _visible_names(names) if name not in assigned]
        assigned.update(visible)
        if visible:
            lines.append("【{}】{}".format(category, " ".join("/" + name for name in visible)))
    lines.append("问具体命令用法：get_bot_help(\"命令名\")")
    return "ok", None, "\n".join(lines)


def _help_command_line(name, info):
    """完整菜单用的紧凑单行：命令名 + 一句话说明（详细用法走 /help 命令名）。"""
    summary = str(info.get("help") or "").strip() or "暂无说明"
    return "/{} {}".format(name, summary)


def _help_command_text(name, info, bot_role, group_id):
    detail_name = _COMMAND_DETAIL_ALIASES.get(name, name)
    detail = COMMAND_DETAILS.get(detail_name)
    if not detail:
        summary = str(info.get("help") or "暂无详细说明").strip()
        match = re.search(r"(/[^\n，。；]+)", summary)
        if match:
            usage = match.group(1).strip()
            description = (summary[:match.start()] + summary[match.end():]).strip(" ：，。；")
        else:
            usage = "/{} [参数]".format(name)
            description = summary
        detail = "格式：{}\n说明：{}\n格式示例：{}".format(
            usage, description or summary, usage)
    usage = detail.strip()
    permission = _help_permission_label(info)
    scope = "群聊" if group_id else "私聊"
    requirements = []
    if info.get("bot_admin_required"):
        requirements.append("小汐需为群管理")
    if info.get("bot_owner_required"):
        requirements.append("小汐需为群主")
    if requirements and bot_role not in ("admin", "owner"):
        status = "当前不可用：" + "、".join(requirements)
    else:
        status = "当前可用"
    return "【/{}】\n{}\n场景：{}｜身份：{}｜{}".format(
        name, usage, scope, permission, status)


async def cmd_help(d, group_id, user_id, args, role, sender_card, message):
    query = args.strip().lstrip("/").lower()
    caller_level, caller_name = await get_user_level(d, group_id, user_id, role)
    bot_role = "member"
    if group_id:
        bot_role, _ = await get_bot_role(d, group_id)

    if query and query not in {name.lower() for name in _HELP_CATEGORIES}:
        status, matched, text = build_help_digest(
            d.commands, caller_level, query, group_id=group_id, bot_role=bot_role)
        if status == "not_found":
            await d._reply(group_id, user_id, "没有这个命令，发 /help 看全部命令")
            return
        if status == "denied":
            await d._reply(group_id, user_id, "这个功能不在你当前身份的菜单里")
            return
        await d._reply(group_id, user_id, text, title="/{} 的用法".format(matched),
                       role_hint=role)
        return

    selected_category = None
    if query:
        selected_category = next(
            (name for name in _HELP_CATEGORIES if name.lower() == query), None)
        if not selected_category:
            aliases = {
                "消息": "聊天与互动", "互动": "聊天与互动", "查询": "娱乐与查询",
                "媒体": "图片与媒体", "成员": "群资料与成员", "文件": "文件与内容",
                "群管": "群管理", "自动化": "功能与自动化", "账号": "主人与维护",
                "实验": "主人与维护",
            }
            selected_category = aliases.get(query)

    role_names = {
        LEVEL_SUPER: "最高主人", LEVEL_MASTER: "群主人", LEVEL_GOWNER: "QQ群主",
        LEVEL_ADMIN: "管理员", LEVEL_MEMBER: "普通群友",
    }
    header = "场景：{}\n身份：{}\n小汐在本群：{}\n下面只放你现在能用或需要知道的功能。".format(
        "群聊" if group_id else "私聊",
        role_names.get(caller_level, caller_name),
        bot_role if group_id else "不适用",
    )
    if not selected_category:
        # 完整菜单用紧凑列表（每条一行）：逐命令详细格式全量超过一万字，
        # 合并转发和普通消息都发不出去；详情走 /help 分类名 或 /help 命令名。
        header += "\n发 /help 分类名 看某类详情，发 /help 命令名 看单个命令用法。"
    sections = [header]
    categories = [selected_category] if selected_category else list(_HELP_CATEGORIES)
    assigned = set()
    for category in categories:
        if not category:
            continue
        names = _HELP_CATEGORIES[category]
        entries = []
        for name, info in d.commands.items():
            if name not in names or name in assigned:
                continue
            if not group_id and caller_level < LEVEL_SUPER and name not in _PRIVATE_VISIBLE:
                continue
            if not _help_visible(info, caller_level, group_id):
                continue
            if selected_category:
                entries.append(_help_command_text(name, info, bot_role, group_id))
            else:
                entries.append(_help_command_line(name, info))
            assigned.add(name)
        if entries:
            joiner = "\n\n" if selected_category else "\n"
            sections.append("【{}】\n{}".format(category, joiner.join(entries)))

    if not selected_category:
        other_entries = []
        for name, info in d.commands.items():
            if name in assigned or not _help_visible(info, caller_level, group_id):
                continue
            if not group_id and caller_level < LEVEL_SUPER and name not in _PRIVATE_VISIBLE:
                continue
            other_entries.append(_help_command_line(name, info))
        if other_entries:
            sections.append("【其他功能】\n" + "\n".join(other_entries))

    title = "小汐的{}帮助".format(selected_category or "完整")
    await d._reply(
        group_id, user_id, "\n\n".join(sections), force_forward=True,
        kind="help", title=title, sections=sections, role_hint=role,
    )
async def cmd_target_group_help(d, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit() or parts[1].strip().lower() not in ("帮助", "help"):
        await d._reply(group_id, user_id, "用法：/群 <群号> 帮助")
        return
    target_group = int(parts[0])
    target_level, _ = await get_user_level(d, target_group, user_id, role)
    if target_level < LEVEL_ADMIN:
        await d._reply(group_id, user_id, "你不是这个群的管理，不能看它的管理菜单")
        return

    class _PrivateHelpProxy:
        def __init__(self, dispatcher):
            self._dispatcher = dispatcher
            self.config = dispatcher.config
            self.client = dispatcher.client
            self.commands = dispatcher.commands

        def __getattr__(self, name):
            return getattr(self._dispatcher, name)

        async def _reply(self, _group_id, target_user, text, **kwargs):
            return await self._dispatcher._reply(None, target_user, text, **kwargs)

    await cmd_help(
        _PrivateHelpProxy(d), target_group, user_id, "", role,
        sender_card, message,
    )


async def cmd_health(d, group_id, user_id, args, role, sender_card, message):
    lines = []
    try:
        bot_state, napcat_state = await asyncio.gather(
            _service_state("qqbot.service"),
            _service_state("napcat.service"),
        )
        lines.append("小汐: " + bot_state)
        lines.append("NapCat: " + napcat_state)
    except Exception as e:
        lines.append("服务状态读取失败: " + str(e))
    try:
        memory = await asyncio.to_thread(_read_linux_memory)
        lines.append("内存: 可用{}M/总{}M".format(memory["available"], memory["total"]))
        lines.append("Swap: 可用{}M/总{}M".format(memory["swap_free"], memory["swap_total"]))
    except (OSError, ValueError, IndexError):
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
            pending = await asyncio.to_thread(load_pending_requests)
            lines.append("待处理申请: {}".format(len(pending)))
        except Exception as error:
            log.debug("Pending request count failed: %s", error)
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
            except ValueError:
                limit = 10
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
        _commit(d, cfg)
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
        _commit(d, cfg)
        await d._reply(group_id, user_id, "安全禁言秒数已设为 " + str(seconds))
        return
    await d._reply(group_id, user_id, "用法：/安全 status | /安全 log | /安全 url on/off | /安全 gray on/off | /安全 punish on/off | /安全 ban 秒数")

async def cmd_clear_ai(d, group_id, user_id, args, role, sender_card, message):
    target_groups, error = resolve_scoped_group_targets(
        d, group_id, user_id, args, allow_all=True, require_configured=True)
    if error:
        await d._reply(group_id, user_id, error)
        return
    code = create_confirmation(
        group_id, user_id, "__clear_group_data__", {"group_ids": target_groups},
        "清理 {} 个群的机器人数据".format(len(target_groups)),
    )
    await d._reply(
        group_id, user_id,
        "这会先创建私有备份，再清除记忆、表情、黑名单和警告。"
        "确认执行请在一分钟内发送 /确认 {}".format(code),
    )

async def cmd_list(d, group_id, user_id, args, role, sender_card, message):
    cfg = d.config
    groups_cfg = cfg.get("groups", {})
    data_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

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
    lines = await asyncio.to_thread(_build_group_data_overview, data_root, target_groups)
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
        buffer = list(getattr(d, "_group_msg_buffer", {}).get(group_id, []))
        if buffer:
            lines = ["NapCat 历史接口不可用，显示机器人进程内最近消息"]
            for _, raw_msg, _, name in buffer[-min(count, 15):]:
                clean = re.sub(r"\[CQ:[^\]]+\]", "", str(raw_msg or "")).strip()
                if clean:
                    lines.append("  {}: {}".format(name or "群友", clean[:60]))
            await d._reply(group_id, user_id, "\n".join(lines)[:2000])
            return
        await d._reply(group_id, user_id, "当前 NapCat 版本无法无游标读取历史消息，且进程内暂无可用缓存")
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
