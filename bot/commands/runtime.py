# bot/commands.py - QQ Bot commands with permission system
import asyncio, json, logging, os, random, re, time
import aiohttp
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from ..permission import (
    get_user_level, get_bot_role, get_group_config,
    add_master, remove_master, list_masters,
    save_group_config, can_moderate_target, LEVEL_MASTER, LEVEL_ADMIN
)
from ..utils import atomic_write_json
log = logging.getLogger("qqbot")
CONFIG_PATH = os.path.join(_ROOT, "config.json")
def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)
def _save(c):
    atomic_write_json(CONFIG_PATH, c, indent=2)
def register_all(d):
    d.register("api", cmd_api_status, "查看 NapCat/OneBot API 能力状态")
    d.register("群信息", cmd_group_info, "查看群信息")
    d.register("成员", cmd_member_info, "查看群成员信息 /成员 QQ号")
    d.register("成员列表", cmd_member_list, "查看群成员列表 /成员列表 [关键词]")
    d.register("文件状态", cmd_file_system_info, "查看群文件存储状态")
    d.register("图片描述", cmd_image_description, "描述图片内容（发送图片或回复图片）")
    d.register("表情回应", cmd_message_reaction, "给回复的消息添加表情 /表情回应 emoji_id")
    d.register("戳", cmd_poke_user, "戳一戳用户 /戳 QQ号")
    d.register("陌生人信息", cmd_stranger_info, "查看 QQ 资料 /陌生人信息 QQ号")
    d.register("好友列表", cmd_friend_list, "查看机器人好友列表", bot_owner_only=True)
    d.register("删除文件", cmd_delete_group_file, "删除群文件 /删除文件 file_id busid",
               admin_only=True, bot_admin_required=True)
    d.register("新建文件夹", cmd_create_group_folder, "新建群文件夹 /新建文件夹 名称",
               admin_only=True, bot_admin_required=True)
    d.register("删除文件夹", cmd_delete_group_folder, "删除群文件夹 /删除文件夹 folder_id",
               admin_only=True, bot_admin_required=True)
    d.register("移动文件", cmd_move_group_file, "移动群文件 /移动文件 file_id 当前目录 目标目录",
               admin_only=True, bot_admin_required=True)
    d.register("重命名文件", cmd_rename_group_file, "重命名群文件 /重命名文件 file_id 当前目录 新名称",
               admin_only=True, bot_admin_required=True)
    d.register("删公告", cmd_delete_group_notice, "删除群公告 /删公告 notice_id",
               admin_only=True, bot_admin_required=True)
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
    d.register("生图", cmd_generate_image, "AI 生成图片 /生图 提示词")
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
    # Title self-service (any member; silently ignored when bot is not group owner)
    d.register("mytitle", cmd_my_title, "我要头衔xxx 给自己设置专属头衔")
    # AI switches (bot owner / bot account only)
    d.register("私聊ai", cmd_private_ai_switch, "私聊AI开关 /私聊AI on/off/allow/deny",
               bot_owner=True)
    d.register("ai聊天", cmd_group_ai_switch, "本群AI聊天开关 /AI聊天 on/off",
               bot_owner=True)
    # uapis.cn fun commands (everyone)
    d.register("天气", cmd_weather, "真实天气 /天气 城市")
    d.register("热榜", cmd_hotboard, "热榜 /热榜 [平台]")
    d.register("热搜", cmd_hotboard, "热榜(别名)")
    d.register("一言", cmd_saying, "随机一言")
    d.register("答案之书", cmd_answerbook, "答案之书 /答案之书 [问题]")
    d.register("每日新闻", cmd_daily_news, "每日新闻图")
    d.register("必应壁纸", cmd_bing_wallpaper, "每日必应壁纸")
    d.register("epic免费", cmd_epic_free, "Epic免费游戏")
    # admin commands
    d.register("全体", cmd_at_all, "@全体成员 /全体 内容",
               admin_only=True, bot_admin_required=True)
    d.register("acg图", cmd_acg_switch, "每日ACG图推送开关 /acg图 on/off",
               admin_only=True)
    d.register("热榜推送", cmd_hotboard_switch, "每日热榜推送开关 /热榜推送 on/off",
               admin_only=True)
    d.register("b站解析", cmd_bili_parse_switch, "B站视频自动解析开关 /b站解析 on/off",
               admin_only=True)
    d.register("gal", cmd_touchgal, "查询Galgame资源页 /gal 作品名")
    d.register("galgame", cmd_touchgal, "查询Galgame资源页 /galgame 作品名")
    d.register("游戏资源", cmd_touchgal, "查询Galgame资源页 /游戏资源 作品名")
    d.register("gal资源", cmd_touchgal_switch, "Galgame资源自动回复开关 /gal资源 on/off",
               admin_only=True)
    # master commands
    d.register("b站推送", cmd_bili_push, "盯UP主新投稿 /b站推送 add/del/list",
               bot_owner=True)
    d.register("积分", cmd_uapi_status, "查看uapis积分额度",
               bot_owner=True)
# ==================== UAPIS FUN COMMANDS ====================
async def cmd_hotboard(d, group_id, user_id, args, role, sender_card, message):
    """/热榜 [平台] — real hot board via uapis.cn."""
    from ..scheduler import BOARD_NAMES, format_hotboard, ai_hotboard_summary
    alias = {v: k for k, v in BOARD_NAMES.items()}
    board = (args.strip().lower() or "weibo")
    board = alias.get(board, board)
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    data = await _uapi.uapi_get(d, "/misc/hotboard", params={"type": board}, kind="user")
    items = (data or {}).get("list") if isinstance(data, dict) else None
    if not items:
        await d._reply(group_id, user_id,
                       "没查到，支持的平台：" + "、".join(BOARD_NAMES.values()))
        return
    summary = await ai_hotboard_summary(d, board, items)
    await d._reply(group_id, user_id, format_hotboard(board, items, summary=summary))


async def cmd_saying(d, group_id, user_id, args, role, sender_card, message):
    """/一言 — random quote via uapis.cn."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    data = await _uapi.uapi_get(d, "/saying/random", kind="user")
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
    if not _uapi.credits_available(d.config, "user"):
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


async def _send_uapi_image(d, group_id, user_id, path, params, label):
    """Download a uapis.cn image endpoint to tmp and send it (bounded)."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    result = await _uapi.uapi_get_binary(d, path, params=params, kind="user")
    if not result:
        await d._reply(group_id, user_id, label + "没取到，等会再试")
        return
    payload, ctype = result
    ext = ".png" if "png" in ctype else ".jpg"
    tmp_dir = os.path.join(_ROOT, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, "uapi_{}{}".format(int(time.time() * 1000), ext))
    try:
        with open(tmp_path, "wb") as f:
            f.write(payload)
        segments = [{"type": "image", "data": {"file": "file://" + tmp_path}}]
        if group_id:
            await d.client.send_group_msg(group_id, segments)
        else:
            await d.client.send_private_msg(user_id, segments)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


async def cmd_daily_news(d, group_id, user_id, args, role, sender_card, message):
    """/每日新闻 — daily news image via uapis.cn."""
    await _send_uapi_image(d, group_id, user_id, "/daily/news-image", None, "每日新闻")


async def cmd_bing_wallpaper(d, group_id, user_id, args, role, sender_card, message):
    """/必应壁纸 — Bing daily wallpaper via uapis.cn."""
    await _send_uapi_image(d, group_id, user_id, "/image/bing-daily", None, "必应壁纸")


async def cmd_epic_free(d, group_id, user_id, args, role, sender_card, message):
    """/epic免费 — Epic free games via uapis.cn."""
    from .. import uapi as _uapi
    if not _uapi.credits_available(d.config, "user"):
        await d._reply(group_id, user_id, "今日积分额度用完了，明天再来")
        return
    data = await _uapi.uapi_get(d, "/game/epic-free", kind="user")
    games = (data or {}).get("data") if isinstance(data, dict) else None
    if not games:
        await d._reply(group_id, user_id, "没查到 Epic 免费游戏，等会再试")
        return
    lines = ["【Epic 免费游戏】"]
    for g in games[:5]:
        title = str(g.get("title") or "?")
        price = str(g.get("original_price_desc") or "")
        state = "限免中" if g.get("is_free_now") else "即将限免"
        lines.append("· {}（{}，{}）".format(title, price, state))
    await d._reply(group_id, user_id, "\n".join(lines))


# ==================== AT ALL / FEATURE SWITCHES / BILI PUSH ====================
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
    log.info("AT_ALL group=%s user=%s text=%s", group_id, user_id, content[:40])


async def _toggle_group_feature(d, group_id, user_id, args, feature, label, cmd_name):
    """Shared on/off toggle for per-group feature flags."""
    if not group_id:
        await d._reply(None, user_id,
                       "这个要在群里用，私聊就带上群号：/{} 群号 on".format(cmd_name))
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
    _save(cfg)
    d.config = cfg
    await d._reply(group_id, user_id,
                   "本群{}已{}".format(label, "开启" if feats[feature] else "关闭"))


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
        _save(cfg)
        d.config = cfg
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
            _save(cfg)
            d.config = cfg
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


async def cmd_uapi_status(d, group_id, user_id, args, role, sender_card, message):
    """/积分 — show uapis.cn credit budget usage."""
    from .. import uapi as _uapi
    info = _uapi.credits_remaining(d.config)
    lines = [
        "【uapis 积分额度】",
        "今日命令：剩 {}/{}".format(info["user_left"], info["user_cap"]),
        "今日预留（自动任务）：剩 {}/{}".format(info["auto_left"], info["auto_cap"]),
        "本月累计：已用 {}，上限 {}".format(
            info["month_cap"] - info["month_left"], info["month_cap"]),
    ]
    await d._reply(group_id, user_id, "\n".join(lines))


# ==================== HELP ====================
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
async def cmd_poke_user(d, group_id, user_id, args, role, sender_card, message):
    mentions = d._extract_mentions(message) if group_id else []
    target = mentions[0] if mentions else (int(args.strip()) if args.strip().isdigit() else user_id)
    result = await (d.client.group_poke(group_id, target) if group_id else d.client.friend_poke(target))
    if result.get("status") != "ok":
        await d._reply(group_id, user_id, "戳一戳失败：" + str(result.get("msg") or result.get("wording") or result)[:180])
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
COMMAND_DETAILS = {
    "天气": "/天气 <城市>\n查真实天气（数据来自高德）。\n例：/天气 杭州",
    "热榜": "/热榜 [平台]（别名 /热搜）\n看指定平台的实时热榜前 10：AI 先用一两句话概括热点趋势，每条带可点击的详情链接。\n平台可写：微博、知乎、B站、抖音、百度、头条、IT之家、V2EX、GitHub、36氪、豆瓣电影\n例：/热榜 微博（不写平台默认微博）",
    "一言": "/一言\n随机来一句语录。",
    "答案之书": "/答案之书 [问题]\n心里想着问题，翻翻答案之书。\n例：/答案之书 今天适合摸鱼吗",
    "每日新闻": "/每日新闻\n发一张今日新闻速览图。",
    "必应壁纸": "/必应壁纸\n发今天的必应壁纸。",
    "epic免费": "/epic免费\n看 Epic 现在在送什么游戏。",
    "全体": "/全体 <内容>\n@全体成员发一条消息（每天次数有限，QQ 限制）。\n例：/全体 今晚八点开黑\n需要：你是本群管理/群主/主人，且 Bot 是管理或群主。",
    "acg图": "/acg图 on 或 /acg图 off\n开关本群的每日 ACG 图推送（每天 0/6/12/18 点，以合并转发形式发 50 张随机二次元图，自动记住发过的图尽量不重复）。\n不写参数可查看当前状态。管理员及以上可用。",
    "热榜推送": "/热榜推送 on 或 /热榜推送 off\n开关本群的每日热榜推送（每天 9 点和 21 点自动发）。\n不写参数可查看当前状态。管理员及以上可用。",
    "b站解析": "/b站解析 on 或 /b站解析 off\n开关本群的 B站视频自动解析：有人发 BV号/av号/b23 链接时，自动回复视频信息并尽量发出视频本体。\n不写参数可查看当前状态。管理员及以上可用。",
    "b站推送": "/b站推送 add <UP主的mid> — 盯一个 UP 主，新投稿和新动态约 1 分钟内推到本群\n/b站推送 del <mid> — 不盯了\n/b站推送 list — 看本群在盯谁\n\nmid 是什么：UP 主空间网址 space.bilibili.com/ 后面的那串数字，直接贴空间链接也行。\n例：/b站推送 add 946974 或 /b站推送 add space.bilibili.com/946974\n视频投稿推送直接生效；动态推送需要提供cookie的B站账号也关注了这个UP主。\n只有群主人和总主人能用。",
    "积分": "/积分\n看 uapis 接口的积分额度用量（每天 100：命令 70 + 自动任务预留 30，每月 1 号重置）。",
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
        await d._reply(group_id, user_id, "这样用：/天气 城市名，比如 /天气 杭州")
        return
    from .. import uapi as _uapi
    data = None
    if _uapi.credits_available(d.config, "user"):
        data = await _uapi.uapi_get(d, "/misc/weather",
                                    params={"city": city[:20]}, kind="user")
    if data:
        reply = "【{}】{}，{}℃，{}，湿度{}%（{}）".format(
            data.get("city") or city,
            data.get("weather", "?"),
            data.get("temperature", "?"),
            "{} {}".format(data.get("wind_direction", ""), data.get("wind_power", "")).strip() or "微风",
            data.get("humidity", "?"),
            data.get("report_time", ""),
        )
    else:
        from ..ai import deepseek_chat
        reply = await deepseek_chat(d, "查询" + city + "今天天气，给出温度、天气状况、穿衣建议。简短一句话。")
    await d._reply(group_id, user_id, reply)
# ==================== TRANSLATE ====================
async def cmd_translate(d, group_id, user_id, args, role, sender_card, message):
    text = args.strip()
    if not text:
        await d._reply(group_id, user_id, "这样用：/translate 要翻译的文本")
        return
    # Try NapCat native translation first (free, fast)
    try:
        result = await d.client.call("translate_en2zh", {"text": text})
        if result.get("status") == "ok":
            data = result.get("data", {})
            translated = data.get("result") or data.get("text") or data.get("translated") or ""
            if translated:
                await d._reply(group_id, user_id, translated[:500])
                return
    except Exception:
        pass
    # Fallback to DeepSeek
    from ..ai import deepseek_chat
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
    from ..ai import deepseek_chat
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
        target_ok, target_error = await can_moderate_target(d, group_id, user_id, tid, role)
        if not target_ok:
            await d._reply(group_id, user_id, target_error)
            continue
        r = await d.client.set_group_kick(group_id, tid, False)
        if r.get("status") == "ok":
            log.warning("ADMIN_ACTION actor=%s group=%s action=kick target=%s", user_id, group_id, tid)
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
        target_ok, target_error = await can_moderate_target(d, group_id, user_id, tid, role)
        if not target_ok:
            await d._reply(group_id, user_id, target_error)
            continue
        r = await d.client.set_group_ban(group_id, tid, duration * 60)
        if r.get("status") == "ok":
            log.warning("ADMIN_ACTION actor=%s group=%s action=ban target=%s duration=%s",
                        user_id, group_id, tid, duration * 60)
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
        target_ok, target_error = await can_moderate_target(d, group_id, user_id, tid, role)
        if not target_ok:
            await d._reply(group_id, user_id, target_error)
            continue
        r = await d.client.set_group_ban(group_id, tid, 0)
        if r.get("status") == "ok":
            log.warning("ADMIN_ACTION actor=%s group=%s action=unban target=%s", user_id, group_id, tid)
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
    if result.get("status") != "ok" and title:
        # A timeout may still have applied server-side; verify before failing.
        try:
            info = await d.client.get_group_member_info(group_id, target)
            if (info.get("data") or {}).get("title", "") == title:
                result = {"status": "ok"}
        except Exception:
            pass
    if result.get("status") == "ok":
        from ..notice_handler import mark_title_set_by_bot
        mark_title_set_by_bot(group_id, target, title)
        await d._reply(group_id, user_id, "头衔设好了" if title else "头衔清掉了")
    else:
        await d._reply(group_id, user_id, "没设成：" + str(result.get("msg") or result.get("wording") or result)[:200])
# ==================== MY TITLE (self-service) ====================
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
    log.info("mytitle: g=%s u=%s title=%s result=%s",
             group_id, user_id, title[:20], str(result)[:200])
    if result.get("status") != "ok":
        # A timeout may still have applied server-side; verify before failing.
        try:
            info = await d.client.get_group_member_info(group_id, user_id)
            if (info.get("data") or {}).get("title", "") == title:
                result = {"status": "ok"}
        except Exception:
            pass
    if result.get("status") == "ok":
        from ..notice_handler import mark_title_set_by_bot
        mark_title_set_by_bot(group_id, user_id, title)
        await d._reply(group_id, user_id, "搞定，你的头衔现在是「" + title + "」了")
    else:
        err = str(result.get("msg") or result.get("wording") or "未知原因")[:120]
        await d._reply(group_id, user_id, "没设成：" + err)
# ==================== AI SWITCHES ====================
async def cmd_group_ai_switch(d, group_id, user_id, args, role, sender_card, message):
    """/AI聊天 on|off — toggle AI chat for this group (owner/bot account only)."""
    if not group_id:
        await d._reply(None, user_id, "这个要在群里用，私聊就带上群号：/AI聊天 群号 on")
        return
    arg = args.strip().lower()
    cfg = _load()
    gid = str(group_id)
    groups = cfg.setdefault("groups", {})
    group_cfg = groups.setdefault(gid, {"enabled": True, "masters": [],
                                        "welcome_msg": {}, "bad_words": {}, "features": {}})
    feats = group_cfg.setdefault("features", {})
    current = feats.get("ai_chat", True)
    if arg not in ("on", "off"):
        await d._reply(group_id, user_id,
                       "本群AI聊天：" + ("开启" if current else "关闭") + "\n用法：/AI聊天 on 或 /AI聊天 off")
        return
    feats["ai_chat"] = (arg == "on")
    _save(cfg)
    d.config = cfg
    await d._reply(group_id, user_id, "本群AI聊天已" + ("开启" if feats["ai_chat"] else "关闭"))
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
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "私聊AI已开启，所有好友都能聊了")
    elif action == "off":
        pc["enabled"] = False
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "私聊AI已关闭，只有开放名单里的人能聊")
    elif action == "allow" and len(parts) >= 2 and parts[1].isdigit():
        qq = int(parts[1])
        allowed = [int(u) for u in allowed if str(u).isdigit()]
        if qq not in allowed:
            allowed.append(qq)
        pc["allowed_users"] = allowed[-50:]
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "已开放私聊AI：" + str(qq))
    elif action == "deny" and len(parts) >= 2 and parts[1].isdigit():
        qq = int(parts[1])
        pc["allowed_users"] = [int(u) for u in allowed if str(u).isdigit() and int(u) != qq]
        _save(cfg)
        d.config = cfg
        await d._reply(group_id, user_id, "已移出开放名单：" + str(qq))
    else:
        status_text = "开启" if pc.get("enabled") else "关闭"
        users = ", ".join(str(u) for u in pc.get("allowed_users", [])) or "无"
        await d._reply(group_id, user_id,
                       "私聊AI：" + status_text + "\n开放名单：" + users +
                       "\n用法：/私聊AI on|off|allow QQ|deny QQ")
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
        cleanup = getattr(d, "_cleanup_stale_state", None)
        if cleanup:
            cleanup()
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
    from ..ai import deepseek_chat
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
# ==================== IMAGE GENERATION ====================
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
