# bot/commands.py - QQ Bot commands with permission system
import asyncio, json, logging, os, random, re, time
import aiohttp

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from .permission import (
    get_user_level, get_bot_role, get_group_config,
    add_master, remove_master, list_masters,
    save_group_config, LEVEL_MASTER, LEVEL_ADMIN
)
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
CONFIG_PATH = os.path.join(_ROOT, "config.json")


def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(c):
    atomic_write_json(CONFIG_PATH, c, indent=2)


def register_all(d):
    # Basic commands
    d.register("help", cmd_help, "查看可用命令")
    d.register("like", cmd_like, "给用户点赞")
    d.register("rank", cmd_rank, "查看发言排行")
    d.register("weather", cmd_weather, "查询天气 /weather 城市")
    d.register("translate", cmd_translate, "翻译文本 /translate 文本")
    d.register("calc", cmd_calc, "计算器 /calc 1+2*3")
    d.register("fortune", cmd_fortune, "今日运势 /fortune")
    d.register("ocr", cmd_ocr, "识别图片文字 /ocr 或回复图片")
    d.register("转发摘要", cmd_forward_summary, "总结合并转发 /转发摘要")
    d.register("群文件", cmd_group_files, "查看群文件 /群文件 [关键词]")
    d.register("文件链接", cmd_group_file_url, "获取群文件链接 /文件链接 file_id busid")
    d.register("精华列表", cmd_essence_list, "查看群精华")
    d.register("群荣誉", cmd_group_honor, "查看群荣誉")
    d.register("已读", cmd_mark_read, "标记消息已读")
    d.register("history", cmd_history, "查看最近消息 /history [数量]")
    d.register("禁言列表", cmd_shut_list, "查看当前被禁言的人")
    d.register("info", cmd_info, "查看成员信息 /info [@用户] 或 /info QQ号")
    d.register("转发", cmd_forward_msg, "转发消息 (回复消息使用)")
    d.register("setgroupavatar", cmd_set_group_avatar, "设置群头像 (回复图片)",
               admin_only=True, bot_admin_required=True)
    d.register("sysmsg", cmd_sysmsg, "查看入群申请/邀请列表", bot_owner=True)
    d.register("点赞信息", cmd_profile_like, "查看机器人点赞统计")
    d.register("health", cmd_health, "查看运行状态")
    d.register("安全", cmd_security, "安全功能 /安全 status|log|url on/off|gray on/off",
               admin_only=True)


    # Admin commands (require bot to be admin/owner)
    d.register("kick", cmd_kick, "踢出成员 /kick @用户",
               admin_only=True, bot_admin_required=True)
    d.register("ban", cmd_ban, "禁言成员 /ban @用户 [分钟]",
               admin_only=True, bot_admin_required=True)
    d.register("unban", cmd_unban, "解除禁言 /unban @用户",
               admin_only=True, bot_admin_required=True)
    d.register("allban", cmd_allban, "全员禁言开关 /allban on/off",
               admin_only=True, bot_admin_required=True)
    d.register("welcome", cmd_welcome, "入群欢迎设置",
               admin_only=True, bot_admin_required=True)
    d.register("badword", cmd_badword, "违禁词设置",
               admin_only=True, bot_admin_required=True)
    d.register("精华", cmd_set_essence, "把回复的消息设为精华",
               admin_only=True, bot_admin_required=True)
    d.register("删精华", cmd_delete_essence, "删除精华消息",
               admin_only=True, bot_admin_required=True)
    d.register("公告", cmd_group_notice, "发布/查看群公告",
               admin_only=True, bot_admin_required=True)
    d.register("clearai", cmd_clear_ai, "清除本群机器人数据",
               bot_owner=True)

    d.register("admin", cmd_admin_mgr, "设置或取消群管理员 /admin add/del @用户",
               admin_only=True, bot_admin_required=True)
    d.register("title", cmd_special_title, "设置专属头衔 /title @用户 头衔",
               admin_only=True, bot_owner_required=True)
    d.register("头衔", cmd_special_title, "设置专属头衔 /头衔 @用户 头衔",
               admin_only=True, bot_owner_required=True)

    # Master management (bot_owner only)
    d.register("master", cmd_master, "管理群主人 /master add/del/list",
               bot_owner_only=True)
    d.register("approve", cmd_approve_request, "同意好友/入群请求",
               bot_owner_only=True)
    d.register("reject", cmd_reject_request, "拒绝好友/入群请求",
               bot_owner_only=True)

    # System (bot_owner only)
    d.register("enable", cmd_enable, "开启群聊机器人", bot_owner=True)
    d.register("disable", cmd_disable, "关闭群聊机器人", bot_owner=True)
    d.register("list", cmd_list, "查看群聊数据概览", bot_owner=True)


# ==================== HELP ====================

async def cmd_help(d, group_id, user_id, args, role, sender_card, message):
    caller_level, caller_name = await get_user_level(d, group_id, user_id, role)
    bot_owner = d.config.get("bot_owner")
    is_owner = (user_id == bot_owner)
    bot_role_str = "member"
    if group_id:
        bot_role_str, _ = await get_bot_role(d, group_id)

    lines = []
    lines.append("* ====== 小汐的使用指南 ====== *")

    # ---- Basic commands (everyone) ----
    lines.append("")
    lines.append("【基础功能】")
    lines.append("  点歌+歌名                   搜索并分享音乐")
    lines.append("  /fortune        /今日运势   每日运势")
    lines.append("  /rank           /水群排行   本周发言排行")
    lines.append("  /like           /赞我       点赞(每日1次)")
    lines.append("  /weather <城市>             查询天气")
    lines.append("  /translate <文本>           翻译成中文")
    lines.append("  /calc <算式>                计算器")
    lines.append("  /ocr                       识别图片文字")
    lines.append("  /转发摘要                  摘要合并转发")
    lines.append("  /群文件 [关键词]            查看群文件")
    lines.append("  /health                    查看运行状态")
    lines.append("  /精华列表 /群荣誉           查看群内容")

    # ---- AI Chat ----
    lines.append("")
    lines.append("【AI 聊天】")
    lines.append("  @小汐 + 任意文字             跟我聊天吧~")
    lines.append("")
    lines.append("【信息查询】")
    lines.append("  /info [@用户或QQ号]          查看成员信息")
    lines.append("  /history [数量]              查看最近消息")
    lines.append("  /禁言列表                    查看被禁言的人")
    lines.append("  /点赞信息                    查看机器人点赞统计")
    lines.append("  /转发 (回复消息)             转发某条消息")

    # ---- Admin commands ----
    if caller_level >= LEVEL_ADMIN and bot_role_str in ("admin", "owner"):
        lines.append("")
        lines.append("【管理命令 - 管理员/群主可用】")
        lines.append("  /kick @某人    踢@某人      踢出群")
        lines.append("  /ban @某人 [分钟] 禁@某人   禁言(默认30分钟)")
        lines.append("  /unban @某人   解@某人      解除禁言")
        lines.append("  /allban on/off              全员禁言开关")
        lines.append("  /welcome on/off/内容         入群欢迎语")
        lines.append("  /badword add/del/list        违禁词管理")
        lines.append("  /精华          回复消息设为精华")
        lines.append("  /公告 内容     发布群公告")
        lines.append("  /title @某人 头衔            专属头衔(机器人必须是群主)")
        lines.append("  /安全 status/log/url/gray    安全功能")

    # ---- Master commands ----
    if caller_level >= LEVEL_MASTER and (is_owner or caller_level == LEVEL_MASTER):
        # Only show if user is actually a master (not just admin)
        if is_owner or caller_level >= LEVEL_MASTER:
            lines.append("")
            lines.append("【群主人命令】")
            lines.append("  /enable         开启本群")
            lines.append("  /disable        关闭本群")
            lines.append("  /list           查看群数据")
            lines.append("  /clearai        清除本群数据")

    # ---- Bot owner only ----
    if is_owner:
        lines.append("")
        lines.append("【最高主人命令】")
        lines.append("  /master add/del/list        管理群主人")
        lines.append("  /approve flag /reject flag  处理入群/好友请求")
        lines.append("  私聊跨群：多数管理命令可写 群号 + 参数")

    lines.append("")
    lines.append("* ============================ *")

    await d._reply(group_id, user_id, "\n".join(lines))


# ==================== LIKE ====================

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
        d._daily_likes[key] = True
        d.save_runtime_state(force=True)
        err = r.get("msg", "") or r.get("wording", "") or str(r)
        await d._reply(group_id, user_id, "没点上，原因是：" + str(err))


# ==================== RANK ====================

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


# ==================== WEATHER ====================

async def cmd_weather(d, group_id, user_id, args, role, sender_card, message):
    city = args.strip()
    if not city:
        await d._reply(group_id, user_id, "这样用：/weather 城市名，比如 /weather 杭州")
        return
    from .ai import deepseek_chat
    reply = await deepseek_chat(d, "查询" + city + "今天天气，给出温度、天气状况、穿衣建议。简短一句话。")
    await d._reply(group_id, user_id, reply)


# ==================== TRANSLATE ====================

async def cmd_translate(d, group_id, user_id, args, role, sender_card, message):
    text = args.strip()
    if not text:
        await d._reply(group_id, user_id, "这样用：/translate 要翻译的文本")
        return
    from .ai import deepseek_chat
    reply = await deepseek_chat(d, "请将以下文本翻译成中文，只给出翻译结果：" + text)
    await d._reply(group_id, user_id, reply)


# ==================== CALC ====================

async def cmd_calc(d, group_id, user_id, args, role, sender_card, message):
    expr = args.strip()
    if not expr:
        await d._reply(group_id, user_id, "这样用：/calc 1+2*3")
        return
    try:
        result = _safe_calc(expr)
        await d._reply(group_id, user_id, expr + " = " + str(result))
    except Exception:
        await d._reply(group_id, user_id, "算不出来，表达式可能不太对")


def _safe_calc(expr):
    import ast
    import operator

    if len(expr) > 80:
        raise ValueError("expression too long")
    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 6:
                raise ValueError("power too large")
            value = ops[type(node.op)](left, right)
            if abs(value) > 1_000_000_000_000:
                raise ValueError("result too large")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.operand))
        raise ValueError("unsupported expression")

    tree = ast.parse(expr, mode="eval")
    result = _eval(tree)
    if isinstance(result, float):
        return round(result, 8)
    return result


# ==================== ROLE ====================


# ==================== FORTUNE ====================

async def cmd_fortune(d, group_id, user_id, args, role, sender_card, message):
    today = time.strftime("%Y%m%d")
    key = today + ":" + str(user_id)
    if key in d._daily_fortunes:
        await d._reply(group_id, user_id, "今天已经看过啦，明天再来")
        return

    d._daily_fortunes[key] = True
    d.save_runtime_state(force=True)
    from .ai import deepseek_chat
    prompt = ("请为星座运势生成一段今日运势，包含综合运势、爱情运势、工作/学业运，"
              "每项一句话，语气像普通群友，简短4-5行即可。")
    reply = await deepseek_chat(d, prompt)
    if reply:
        await d._reply(group_id, user_id, sender_card + " 的今日运势\n\n" + reply)
    else:
        await d._reply(group_id, user_id, "脑子卡了一下，等会再试")


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


# ==================== QQ/NAPCAT MEDIA COMMANDS ====================

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
    from .media import _extract_ocr_text
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
    from .media import describe_forward
    text = await describe_forward(d, forward_seg)
    from .ai import deepseek_chat
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
    log.info("set_essence_msg response: mid=%s result=%s", mid, str(result)[:300])
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


async def cmd_approve_request(d, group_id, user_id, args, role, sender_card, message):
    flag = args.strip().split(maxsplit=1)[0] if args.strip() else ""
    if not flag:
        await d._reply(group_id, user_id, "这样用：/approve flag")
        return
    from .request_handler import approve_request
    ok, msg = await approve_request(d, flag, True, "")
    await d._reply(group_id, user_id, msg if ok else "处理失败：" + msg)


async def cmd_reject_request(d, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await d._reply(group_id, user_id, "这样用：/reject flag 原因")
        return
    reason = parts[1] if len(parts) > 1 else "不通过"
    from .request_handler import approve_request
    ok, msg = await approve_request(d, parts[0], False, reason)
    await d._reply(group_id, user_id, msg if ok else "处理失败：" + msg)


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
            from .request_handler import load_pending_requests
            lines.append("待处理申请: {}".format(len(load_pending_requests())))
        except Exception:
            pass
    await d._reply(group_id, user_id, "\n".join(lines))


async def cmd_security(d, group_id, user_id, args, role, sender_card, message):
    from .security import security_config

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
        from .security import format_security_events
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



# ==================== KICK ====================

async def cmd_kick(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    mentions = d._extract_mentions(message)
    if not mentions:
        try:
            mentions = [int(args.strip())]
        except ValueError:
            pass
    if not mentions:
        await d._reply(group_id, user_id, "请 @要踢出的人")
        return
    for tid in mentions:
        if tid == d.config["bot_qq"]:
            await d._reply(group_id, user_id, "这个不行，我不能踢自己")
            continue
        r = await d.client.set_group_kick(group_id, tid, False)
        if r.get("status") == "ok":
            await d._reply(group_id, user_id, "踢掉了：" + str(tid))
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没踢掉 " + str(tid) + "，原因是：" + str(err))


# ==================== BAN ====================

async def cmd_ban(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    mentions = d._extract_mentions(message)
    clean_args = re.sub(r"\[CQ:[^]]+\]", "", args)
    if not mentions:
        ids = re.findall(r"\b\d{5,12}\b", clean_args)
        mentions = [int(ids[0])] if ids else []
    if not mentions:
        await d._reply(group_id, user_id, "请 @要禁言的人")
        return
    duration = 30
    m = re.search(r"(?<!\d)(\d{1,5})(?!\d)(?:\s*(?:分钟|分|min|m))?", clean_args)
    if m:
        duration = max(1, min(int(m.group(1)), 43200))
    for tid in mentions:
        if tid == d.config["bot_qq"]:
            await d._reply(group_id, user_id, "这个不行，我不能禁言自己")
            continue
        r = await d.client.set_group_ban(group_id, tid, duration * 60)
        if r.get("status") == "ok":
            await d._reply(group_id, user_id, "禁言了：" + str(tid) + "，" + str(duration) + " 分钟")
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没禁言成功，原因是：" + str(err))


# ==================== UNBAN ====================

async def cmd_unban(d, group_id, user_id, args, role, sender_card, message):
    if not group_id:
        return
    mentions = d._extract_mentions(message)
    if not mentions:
        clean_args = re.sub(r"\[CQ:[^]]+\]", "", args)
        ids = re.findall(r"\b\d{5,12}\b", clean_args)
        mentions = [int(ids[0])] if ids else []
    if not mentions:
        await d._reply(group_id, user_id, "请 @要解禁的人")
        return
    for tid in mentions:
        r = await d.client.set_group_ban(group_id, tid, 0)
        if r.get("status") == "ok":
            await d._reply(group_id, user_id, "解开了")
        else:
            err = r.get("msg", "") or r.get("wording", "") or str(r)
            await d._reply(group_id, user_id, "没解开，原因是：" + str(err))


# ==================== ALLBAN ====================

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


# ==================== ADMIN MANAGEMENT ====================

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
    log.info("set_group_special_title response: %s", str(result)[:300])
    if result.get("status") == "ok":
        await d._reply(group_id, user_id, "头衔设好了" if title else "头衔清掉了")
    else:
        await d._reply(group_id, user_id, "没设成：" + str(result.get("msg") or result.get("wording") or result)[:200])


# ==================== MASTER ====================

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


# ==================== WELCOME ====================

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
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "入群欢迎已开启")
    elif arg == "off":
        w["enabled"] = False
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "入群欢迎已关闭")
    elif arg:
        w["template"] = arg
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "欢迎语改好了：" + arg)
    else:
        status_text = "开启" if w["enabled"] else "关闭"
        await d._reply(group_id, user_id,
                       "入群欢迎状态: " + status_text + "\n当前模板: " + w.get("template", ""))


# ==================== BADWORD ====================

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
            _save(cfg)
            d.config = cfg
            await d._reply(group_id, user_id, "违禁词加好了：" + word)
        else:
            await d._reply(group_id, user_id, "该词已存在")
    elif action == "del" and word:
        if word in bw["words"]:
            bw["words"].remove(word)
            _save(cfg)
            d.config = cfg
            await d._reply(group_id, user_id, "违禁词删掉了：" + word)
        else:
            await d._reply(group_id, user_id, "该词不存在")
    elif action == "on":
        bw["enabled"] = True
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "违禁词过滤已开启")
    elif action == "off":
        bw["enabled"] = False
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "违禁词过滤已关闭")
    else:
        word_list = ", ".join(bw["words"]) if bw["words"] else "(空)"
        status_text = "开启" if bw["enabled"] else "关闭"
        await d._reply(group_id, user_id,
                       "违禁词列表: " + word_list + "\n状态: " + status_text)


# ==================== CLEAR AI ====================

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
        from .ai import clear_group_memory
        clear_group_memory(d, gid)

        # 2. Clear stickers
        import os as _os3
        sticker_path = _os3.path.join(_os3.path.dirname(_os3.path.dirname(_os3.path.abspath(__file__))),
                                    "data", "stickers", f"group_{gid}.json")
        if _os3.path.exists(sticker_path):
            _os3.remove(sticker_path)

        # 3. Clear blacklist entries for this group
        from .guard import load_blacklist, save_blacklist
        bl = load_blacklist()
        prefix = f"{gid}_"
        removed = [k for k in bl if k.startswith(prefix)]
        for k in removed:
            del bl[k]
        if removed:
            save_blacklist(bl)

        # 4. Clear R18 warnings for this group
        try:
            from .guard import load_warnings, save_warnings
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



# ==================== LIST (owner-only) ====================

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

# ==================== ENABLE/DISABLE ====================

async def cmd_enable(d, group_id, user_id, args, role, sender_card, message):
    cfg = _load()
    groups = cfg.setdefault("groups", {})

    # Determine target groups
    target_groups = []
    if not group_id:
        if args.strip():
            target_groups = [g.strip() for g in args.split() if g.strip().isdigit()]
        if not target_groups:
            target_groups = list(cfg.get("groups", {}).keys())
    else:
        target_groups = [str(group_id)]
        if args.strip():
            extra = [g.strip() for g in args.split() if g.strip().isdigit()]
            for g in extra:
                if g not in target_groups:
                    target_groups.append(g)

    if not target_groups:
        await d._reply(group_id, user_id, "这样用：/enable [群号1 群号2 ...]")
        return

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
    _save(cfg)
    d.config = cfg
    msg = f"已启用 {len(enabled_list)} 个群"
    if len(enabled_list) <= 5:
        msg += f": {', '.join(enabled_list)}"
    await d._reply(group_id, user_id, msg + "，我来了")


async def cmd_disable(d, group_id, user_id, args, role, sender_card, message):
    cfg = _load()
    groups = cfg.setdefault("groups", {})

    # Determine target groups
    target_groups = []
    if not group_id:
        if args.strip():
            target_groups = [g.strip() for g in args.split() if g.strip().isdigit()]
        if not target_groups:
            target_groups = list(cfg.get("groups", {}).keys())
    else:
        target_groups = [str(group_id)]
        if args.strip():
            extra = [g.strip() for g in args.split() if g.strip().isdigit()]
            for g in extra:
                if g not in target_groups:
                    target_groups.append(g)

    if not target_groups:
        await d._reply(group_id, user_id, "这样用：/disable [群号1 群号2 ...]")
        return

    disabled_list = []
    for gid in target_groups:
        if gid in groups:
            groups[gid]["enabled"] = False
            disabled_list.append(gid)
    if disabled_list:
        _save(cfg)
        d.config = cfg
        msg = f"已关闭 {len(disabled_list)} 个群"
        if len(disabled_list) <= 5:
            msg += f": {', '.join(disabled_list)}"
        await d._reply(group_id, user_id, msg + "，我先潜了")
    else:
        await d._reply(group_id, user_id, "没找到能关闭的群")


# ==================== MUSIC SEARCH ====================

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
                    log.warning("Music card send failed: %s", r.get("msg", str(r)))
                return True
        except Exception as e:
            log.error("Music parse error: %s", e)

    from .ai import deepseek_chat
    reply = await deepseek_chat(d, "用户想点歌「" + keyword + "」，请用1行推荐一首歌（格式：推荐「歌名 - 歌手」）。不确定就诚实说。")
    await d.client.send_group_msg(group_id, reply)
    return True


# ==================== HISTORY (消息历史) ====================

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


# ==================== SHUT LIST (禁言列表) ====================

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


# ==================== INFO (增强版) ====================

async def cmd_info(d, group_id, user_id, args, role, sender_card, message):
    mentions = d._extract_mentions(message)
    clean_args = re.sub(r"\[CQ:[^]]+\]", "", args).strip()

    # 群内：@用户 或 无参数看自己
    if group_id and not clean_args and not mentions:
        mentions = [user_id]

    if mentions:
        target = mentions[0]
        if group_id:
            r = await d.client.get_group_member_info(group_id, target, no_cache=True)
            if r.get("status") != "ok":
                await d._reply(group_id, user_id, "获取信息失败：" + str(r.get("msg") or r.get("wording") or r)[:200])
                return
            data = r.get("data", {})
            lines = [
                f"QQ: {data.get('user_id', target)}",
                f"昵称: {data.get('nickname', '')}",
            ]
            card = data.get("card", "")
            if card:
                lines.append(f"群名片: {card}")
            title = data.get("title", "")
            if title:
                lines.append(f"专属头衔: {title}")
            role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(data.get("role", ""), "成员")
            lines.append(f"身份: {role_cn}")
            level = data.get("level", "")
            if level:
                lines.append(f"等级: Lv.{level}")
            sex = data.get("sex", "")
            if sex and sex != "unknown":
                lines.append(f"性别: {sex}")
            age = data.get("age", 0)
            if age:
                lines.append(f"年龄: {age}")
            join_time = data.get("join_time", 0)
            if join_time:
                import datetime as _dt
                jt = _dt.datetime.fromtimestamp(join_time).strftime("%Y-%m-%d %H:%M")
                lines.append(f"入群时间: {jt}")
            await d._reply(group_id, user_id, "\n".join(lines))
            return
        else:
            # 私聊：用 get_stranger_info 查任意人
            r = await d.client.get_stranger_info(target, no_cache=True)
            if r.get("status") != "ok":
                await d._reply(group_id, user_id, "获取信息失败：" + str(r.get("msg") or r.get("wording") or r)[:200])
                return
            data = r.get("data", {})
            lines = [
                f"QQ: {data.get('user_id', target)}",
                f"昵称: {data.get('nickname', '')}",
            ]
            sex = data.get("sex", "")
            if sex and sex != "unknown":
                lines.append(f"性别: {sex}")
            age = data.get("age", 0)
            if age:
                lines.append(f"年龄: {age}")
            await d._reply(group_id, user_id, "\n".join(lines))
            return

    # 有 args 但不是 @：解析为 QQ 号
    if clean_args:
        qq_match = re.search(r"\d{5,12}", clean_args)
        if qq_match:
            target = int(qq_match.group())
            if group_id:
                r = await d.client.get_group_member_info(group_id, target, no_cache=True)
                if r.get("status") == "ok":
                    data = r.get("data", {})
                    lines = [f"QQ: {data.get('user_id', target)}", f"昵称: {data.get('nickname', '')}"]
                    card = data.get("card", "")
                    if card:
                        lines.append(f"群名片: {card}")
                    role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(data.get("role", ""), "成员")
                    lines.append(f"身份: {role_cn}")
                    await d._reply(group_id, user_id, "\n".join(lines))
                    return
            # 私聊或群内非成员：用 stranger_info
            r = await d.client.get_stranger_info(target, no_cache=True)
            if r.get("status") == "ok":
                data = r.get("data", {})
                lines = [f"QQ: {data.get('user_id', target)}", f"昵称: {data.get('nickname', '')}"]
                sex = data.get("sex", "")
                if sex and sex != "unknown":
                    lines.append(f"性别: {sex}")
                await d._reply(group_id, user_id, "\n".join(lines))
                return
            await d._reply(group_id, user_id, "获取信息失败")
            return

    await d._reply(group_id, user_id, "用法：/info [@用户] 或 /info QQ号")


# ==================== FORWARD MSG (转发) ====================

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


# ==================== SET GROUP AVATAR (设置群头像) ====================

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


# ==================== SYSMSG (系统消息) ====================

async def cmd_sysmsg(d, group_id, user_id, args, role, sender_card, message):
    from .request_handler import format_pending_requests

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


# ==================== PROFILE LIKE (点赞信息) ====================

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
