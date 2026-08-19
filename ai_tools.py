"""Small, safe AI-facing tool layer for NapCat capabilities."""

import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("qqbot")

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _clip(value, limit=1200):
    return str(value)[:limit]


async def get_group_info(dispatcher, group_id):
    result = await dispatcher.client.get_group_info(group_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_member_info(dispatcher, group_id, user_id):
    result = await dispatcher.client.get_group_member_info(group_id, user_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_recent_messages(dispatcher, group_id, count=10):
    count = max(1, min(int(count), 20))
    result = await dispatcher.client.get_group_msg_history(group_id, count=count)
    data = result.get("data")
    if isinstance(data, dict):
        data = data.get("messages") or data.get("messages_list") or []
    return {"ok": result.get("status") == "ok", "data": data or [],
            "message": result.get("msg") or result.get("wording", "")}


async def get_group_files(dispatcher, group_id, keyword=""):
    result = await dispatcher.client.get_group_root_files(group_id)
    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
    files = data.get("files") or []
    if keyword:
        key = keyword.lower()
        files = [item for item in files if key in str(item.get("file_name") or item.get("name", "")).lower()]
    return {"ok": result.get("status") == "ok", "data": files[:15],
            "message": result.get("msg") or result.get("wording", "")}


async def get_file_url(dispatcher, group_id, file_id, busid):
    result = await dispatcher.client.get_group_file_url(group_id, file_id, busid)
    data = result.get("data", {})
    return {"ok": result.get("status") == "ok", "data": data,
            "message": result.get("msg") or result.get("wording", "")}


async def get_group_notice(dispatcher, group_id):
    result = await dispatcher.client.get_group_notice(group_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_group_honor(dispatcher, group_id, honor_type="all"):
    result = await dispatcher.client.get_group_honor_info(group_id, honor_type)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_shut_list(dispatcher, group_id):
    result = await dispatcher.client.get_group_shut_list(group_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_friend_info(dispatcher, user_id):
    result = await dispatcher.client.get_stranger_info(user_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_image_ocr(dispatcher, image):
    result = await dispatcher.client.ocr_image(image)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_essence_list(dispatcher, group_id):
    result = await dispatcher.client.get_essence_msg_list(group_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_group_info_ex(dispatcher, group_id):
    result = await dispatcher.client.get_group_info_ex(group_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def check_url_safety(dispatcher, url):
    result = await dispatcher.client.check_url_safely(str(url)[:300])
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def translate_text(dispatcher, text):
    result = await dispatcher.client.translate_en2zh(str(text)[:300])
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_at_all_remain(dispatcher, group_id):
    result = await dispatcher.client.get_group_at_all_remain(group_id)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


# ---------- uapis.cn tools (credit-budgeted) ----------

async def uapi_weather(dispatcher, city):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user", path="/misc/weather"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/misc/weather",
                               params={"city": str(city)[:20]}, kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {
        "city": data.get("city"), "weather": data.get("weather"),
        "temperature": data.get("temperature"),
        "wind": "{} {}".format(data.get("wind_direction", ""), data.get("wind_power", "")),
        "humidity": data.get("humidity"), "report_time": data.get("report_time"),
    }}


async def uapi_hotboard(dispatcher, type="weibo"):
    from bot import uapi
    board = str(type or "weibo")[:20]
    if not uapi.credits_available(dispatcher.config, "user", path="/misc/hotboard"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/misc/hotboard",
                               params={"type": board}, kind="user")
    items = (data or {}).get("list") if isinstance(data, dict) else None
    if not items:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {
        "type": board,
        "top": [{"title": i.get("title"), "hot": i.get("hot_value")}
                for i in items[:10]],
    }}


async def uapi_saying(dispatcher):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user", path="/saying"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/saying", kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {
        "content": data.get("content"), "author": data.get("author"),
        "source": data.get("source"),
    }}


async def uapi_answerbook(dispatcher, question=""):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user", path="/answerbook/ask"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/answerbook/ask",
                               params={"question": str(question)[:60] or "今天会发生什么"},
                               kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {"answer": data.get("answer")}}


async def uapi_epic_free(dispatcher):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user", path="/game/epic-free"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/game/epic-free", kind="user")
    games = (data or {}).get("data") if isinstance(data, dict) else None
    if not games:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {
        "games": [{"title": g.get("title"), "price": g.get("original_price_desc"),
                   "free_now": g.get("is_free_now")} for g in games[:5]],
    }}


async def execute_tool(dispatcher, name, arguments):
    """Dispatch only registered low-risk tools; never accepts a raw OneBot action."""
    args = arguments if isinstance(arguments, dict) else {}
    tools = {
        "get_group_info": get_group_info,
        "get_member_info": get_member_info,
        "get_recent_messages": get_recent_messages,
        "get_group_files": get_group_files,
        "get_file_url": get_file_url,
        "get_group_notice": get_group_notice,
        "get_group_honor": get_group_honor,
        "get_shut_list": get_shut_list,
        "get_friend_info": get_friend_info,
        "ocr_image": get_image_ocr,
        "get_essence_list": get_essence_list,
        "get_group_info_ex": get_group_info_ex,
        "check_url_safely": check_url_safety,
        "translate_en2zh": translate_text,
        "get_group_at_all_remain": get_at_all_remain,
        "uapi_weather": uapi_weather,
        "uapi_hotboard": uapi_hotboard,
        "uapi_saying": uapi_saying,
        "uapi_answerbook": uapi_answerbook,
        "uapi_epic_free": uapi_epic_free,
        "uapi_search": uapi_search,
    }
    handler = tools.get(name)
    if not handler:
        return {"ok": False, "error": "tool_not_allowed", "tool": name}
    # Callers inject context args (e.g. group_id) that plain uapi tools don't
    # accept; drop anything outside the handler signature, same as _wrap_read.
    import inspect as _inspect
    params = _inspect.signature(handler).parameters
    if not any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in params.values()):
        args = {k: v for k, v in args.items() if k in params}
    try:
        result = await handler(dispatcher, **args)
        result["tool"] = name
        return result
    except Exception as exc:
        log.warning("AI tool %s failed: %s", name, exc)
        return {"ok": False, "error": "tool_failed", "tool": name, "message": _clip(exc, 200)}


async def execute_admin_tool(dispatcher, name, arguments, actor_id, sender_role="member"):
    """Execute an allowlisted management tool for a verified admin or owner."""
    from bot.permission import get_user_level, get_bot_role, can_moderate_target, LEVEL_ADMIN
    args = arguments if isinstance(arguments, dict) else {}
    group_id = int(args.get("group_id") or 0)
    target_id = int(args.get("user_id") or 0)
    if not group_id:
        return {"ok": False, "error": "invalid_group", "tool": name}
    if name != "whole_ban" and not target_id:
        return {"ok": False, "error": "invalid_target", "tool": name}
    level, _ = await get_user_level(dispatcher, group_id, actor_id, sender_role)
    if actor_id != dispatcher.config.get("bot_owner") and level < LEVEL_ADMIN:
        return {"ok": False, "error": "permission_denied", "tool": name}
    bot_role, _ = await get_bot_role(dispatcher, group_id)
    if bot_role not in ("admin", "owner"):
        return {"ok": False, "error": "bot_permission_denied", "tool": name}
    if name != "whole_ban":
        allowed, reason = await can_moderate_target(
            dispatcher, group_id, actor_id, target_id, sender_role)
        if not allowed:
            return {"ok": False, "error": "target_not_allowed", "message": reason, "tool": name}
    handlers = {
        "kick_member": lambda: dispatcher.client.set_group_kick(group_id, target_id, False),
        "ban_member": lambda: dispatcher.client.set_group_ban(
            group_id, target_id, max(1, min(int(args.get("duration", 600)), 2592000))),
        "unban_member": lambda: dispatcher.client.set_group_ban(group_id, target_id, 0),
        "whole_ban": lambda: dispatcher.client.set_group_whole_ban(group_id, bool(args.get("enable", True))),
    }
    handler = handlers.get(name)
    if not handler:
        return {"ok": False, "error": "admin_tool_not_allowed", "tool": name}
    result = await handler()
    log.warning("ADMIN_TOOL actor=%s group=%s tool=%s target=%s status=%s retcode=%s",
                actor_id, group_id, name, target_id, result.get("status"), result.get("retcode"))
    return {"ok": result.get("status") == "ok", "tool": name,
            "data": result.get("data"), "message": result.get("msg") or result.get("wording", "")}


# ---------- interaction tools (scene-gated, per-group daily quota) ----------

import time as _time

INTERACTION_DAILY_LIMIT = 30
_interaction_usage = {}  # "YYYYmmdd:group_id" -> count


def _quota_key(group_id):
    return "{}:{}".format(_time.strftime("%Y%m%d"), group_id or 0)


def interaction_quota_left(group_id):
    used = _interaction_usage.get(_quota_key(group_id), 0)
    return max(0, INTERACTION_DAILY_LIMIT - used)


def _record_interaction_usage(group_id):
    key = _quota_key(group_id)
    _interaction_usage[key] = _interaction_usage.get(key, 0) + 1
    if len(_interaction_usage) > 500:
        today = _time.strftime("%Y%m%d")
        for item in list(_interaction_usage):
            if not item.startswith(today):
                _interaction_usage.pop(item, None)


async def execute_interaction_tool(dispatcher, name, arguments,
                                   group_id=0, user_id=0):
    """Execute a scene-gated interaction tool (emoji reaction / like).

    Only exposed to the AI in explicit / follow-up scenes; hard-capped
    per group per day. Never includes management capabilities.
    """
    args = arguments if isinstance(arguments, dict) else {}
    handlers = {
        "set_msg_emoji_like": lambda: dispatcher.client.set_msg_emoji_like(
            int(args.get("message_id") or 0), str(args.get("emoji_id") or "128077")),
        "send_like": lambda: dispatcher.client.send_like(
            int(args.get("user_id") or user_id or 0),
            max(1, min(int(args.get("times") or 1), 10))),
    }
    handler = handlers.get(name)
    if not handler:
        return {"ok": False, "error": "interaction_tool_not_allowed", "tool": name}
    if interaction_quota_left(group_id) <= 0:
        return {"ok": False, "error": "interaction_quota_exhausted", "tool": name}
    try:
        result = await handler()
    except Exception as exc:
        log.warning("interaction tool %s failed: %s", name, exc)
        return {"ok": False, "error": "tool_failed", "tool": name,
                "message": _clip(exc, 200)}
    if result.get("status") == "ok":
        _record_interaction_usage(group_id)
    log.info("INTERACTION_TOOL group=%s user=%s tool=%s status=%s",
             group_id, user_id, name, result.get("status"))
    return {"ok": result.get("status") == "ok", "tool": name,
            "message": result.get("msg") or result.get("wording", "")}


def reset_quota_for_test():
    _interaction_usage.clear()


def format_tool_result(result):
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:1800]


# ==================== TIERED TOOL REGISTRY (native function calling) ====================
# Tiers:
#   read        - safe read-only queries, offered in every scene
#   interaction - visible side effects (emoji/like/music), explicit scenes only,
#                 per-group daily quota
#   playful     - playful_ban only, explicit scenes only, hardcoded constraints
# Kick / unban / whole-ban NEVER enter any tier.

def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


def _p_str(desc):
    return {"type": "string", "description": desc}


def _p_int(desc):
    return {"type": "integer", "description": desc}


# ---------- new read-tier handlers ----------

async def get_group_msg_history(dispatcher, group_id, count=20):
    count = max(1, min(int(count or 20), 50))
    result = await dispatcher.client.get_group_msg_history(group_id, count=count)
    data = result.get("data")
    if isinstance(data, dict):
        data = data.get("messages") or []
    lines = []
    for msg in (data or []):
        sender = msg.get("sender", {}) if isinstance(msg, dict) else {}
        name = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))
        raw = re.sub(r"\[CQ:[^\]]+\]", "", msg.get("raw_message", "") or "").strip()
        if raw:
            lines.append("{}: {}".format(name, raw[:60]))
    return {"ok": result.get("status") == "ok", "data": lines[-20:],
            "message": result.get("msg") or result.get("wording", "")}


async def get_forward_msg(dispatcher, message_id):
    result = await dispatcher.client.get_forward_msg(int(message_id))
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def get_friend_list(dispatcher):
    result = await dispatcher.client.get_friend_list()
    data = result.get("data")
    friends = data
    if isinstance(data, list):
        friends = [{"user_id": f.get("user_id"), "nickname": f.get("nickname"),
                    "remark": f.get("remark")} for f in data[:30]]
    return {"ok": result.get("status") == "ok", "data": friends,
            "message": result.get("msg") or result.get("wording", "")}


async def get_recent_contact(dispatcher, count=10):
    count = max(1, min(int(count or 10), 30))
    result = await dispatcher.client.get_recent_contact(count)
    return {"ok": result.get("status") == "ok", "data": result.get("data"),
            "message": result.get("msg") or result.get("wording", "")}


async def uapi_search(dispatcher, query):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user", path="/search/aggregate"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_post(dispatcher, "/search/aggregate",
                                json_body={"query": str(query)[:80]}, kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": data}


async def uapi_translate(dispatcher, text):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user", path="/translate/text"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_post(dispatcher, "/translate/text",
                                json_body={"text": str(text)[:300]}, kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": data}


# ---------- interaction-tier handlers (args dict + scene context) ----------

async def _tool_set_msg_emoji_like(dispatcher, args, ctx):
    message_id = int(args.get("message_id") or ctx.get("message_id") or 0)
    emoji_id = str(args.get("emoji_id") or "128077")
    result = await dispatcher.client.set_msg_emoji_like(message_id, emoji_id)
    return {"ok": result.get("status") == "ok",
            "message": result.get("msg") or result.get("wording", "")}


async def _tool_send_like(dispatcher, args, ctx):
    uid = int(args.get("user_id") or ctx.get("user_id") or 0)
    times = max(1, min(int(args.get("times") or 1), 10))
    result = await dispatcher.client.send_like(uid, times)
    return {"ok": result.get("status") == "ok",
            "message": result.get("msg") or result.get("wording", "")}


async def _tool_send_music_card(dispatcher, args, ctx):
    """Search NetEase music and send a [CQ:music] card (mirrors 点歌 command)."""
    keyword = str(args.get("keyword") or "").strip()[:40]
    if not keyword:
        return {"ok": False, "error": "missing_keyword"}
    try:
        session = dispatcher.client.session
        url = "https://music.163.com/api/search/get?s=" + keyword + "&type=1&limit=1"
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None) if resp.status == 200 else None
    except Exception as e:
        log.warning("send_music_card search failed: %s", e)
        data = None
    songs = []
    if isinstance(data, dict):
        songs = data.get("result", {}).get("songs", []) or []
    if not songs:
        return {"ok": False, "error": "song_not_found"}
    music_msg = [{"type": "music", "data": {"type": "163", "id": str(songs[0]["id"])}}]
    if ctx.get("group_id"):
        r = await dispatcher.client.send_group_msg(ctx["group_id"], music_msg)
    else:
        r = await dispatcher.client.send_private_msg(ctx.get("user_id"), music_msg)
    return {"ok": r.get("status") == "ok", "data": {"song": songs[0].get("name")},
            "message": r.get("msg") or r.get("wording", "")}


# ---------- playful tier: playful_ban with hardcoded constraints ----------

PLAYFUL_BAN_MAX_DURATION = 120
PLAYFUL_BAN_DAILY_GROUP_LIMIT = 5
PLAYFUL_BAN_COOLDOWN_SECONDS = 60
_PLAYFUL_BAN_AUDIT = os.path.join(_ROOT, "data", "playful_ban_audit.json")
_playful_ban_group_usage = {}   # "YYYYmmdd:group_id" -> count
_playful_ban_target_usage = {}  # "YYYYmmdd:group_id:user_id" -> True
_playful_ban_last_ts = {}       # group_id -> timestamp


def _prune_playful_ban_state(today):
    for dct in (_playful_ban_group_usage, _playful_ban_target_usage):
        if len(dct) > 500:
            for item in list(dct):
                if not item.startswith(today):
                    dct.pop(item, None)


def _audit_playful_ban(record):
    from bot.utils import atomic_write_json
    try:
        entries = []
        if os.path.exists(_PLAYFUL_BAN_AUDIT):
            with open(_PLAYFUL_BAN_AUDIT, encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                entries = []
        entries.append(record)
        atomic_write_json(_PLAYFUL_BAN_AUDIT, entries[-100:], indent=2)
    except Exception as e:
        log.warning("playful_ban audit write failed: %s", e)


async def execute_playful_ban(dispatcher, args, ctx):
    """AI-autonomous playful ban. All constraints below are code-enforced."""
    from bot.permission import get_user_level, get_bot_role, LEVEL_ADMIN
    group_id = int(ctx.get("group_id") or 0)
    if not group_id:
        return {"ok": False, "error": "group_only", "tool": "playful_ban"}
    try:
        target_id = int(args.get("user_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    if not target_id:
        return {"ok": False, "error": "invalid_target", "tool": "playful_ban"}
    try:
        duration = int(args.get("duration") or 30)
    except (TypeError, ValueError):
        duration = 30
    duration = max(1, min(duration, PLAYFUL_BAN_MAX_DURATION))
    reason = str(args.get("reason") or "玩闹")[:50]
    # Target protection: admin level and above (master/gowner/admin/super) is off-limits
    level, _ = await get_user_level(dispatcher, group_id, target_id, "member")
    if level >= LEVEL_ADMIN:
        return {"ok": False, "error": "target_protected", "tool": "playful_ban"}
    bot_role, _ = await get_bot_role(dispatcher, group_id)
    if bot_role not in ("admin", "owner"):
        return {"ok": False, "error": "bot_not_admin", "tool": "playful_ban"}
    today = _time.strftime("%Y%m%d")
    gkey = "{}:{}".format(today, group_id)
    if _playful_ban_group_usage.get(gkey, 0) >= PLAYFUL_BAN_DAILY_GROUP_LIMIT:
        return {"ok": False, "error": "daily_limit_reached", "tool": "playful_ban"}
    tkey = "{}:{}".format(gkey, target_id)
    if tkey in _playful_ban_target_usage:
        return {"ok": False, "error": "target_already_banned_today", "tool": "playful_ban"}
    now = _time.time()
    if now - _playful_ban_last_ts.get(group_id, 0) < PLAYFUL_BAN_COOLDOWN_SECONDS:
        return {"ok": False, "error": "cooldown_active", "tool": "playful_ban"}
    result = await dispatcher.client.set_group_ban(group_id, target_id, duration)
    ok = result.get("status") == "ok"
    if ok:
        _playful_ban_group_usage[gkey] = _playful_ban_group_usage.get(gkey, 0) + 1
        _playful_ban_target_usage[tkey] = True
        _playful_ban_last_ts[group_id] = now
        _prune_playful_ban_state(today)
    log.warning("PLAYFUL_BAN group=%s actor=AI target=%s duration=%ss reason=%s status=%s",
                group_id, target_id, duration, reason, result.get("status"))
    _audit_playful_ban({
        "ts": now, "group_id": group_id, "actor": "AI",
        "target_id": target_id, "duration": duration, "reason": reason,
        "ok": ok,
    })
    return {"ok": ok, "tool": "playful_ban", "duration": duration,
            "message": result.get("msg") or result.get("wording", "")}


def reset_playful_ban_for_test():
    _playful_ban_group_usage.clear()
    _playful_ban_target_usage.clear()
    _playful_ban_last_ts.clear()


# ---------- registry ----------

def _wrap_read(fn):
    # Only pass kwargs the underlying function actually accepts; the executor
    # injects context args (e.g. group_id) that plain uapi tools don't take.
    import inspect as _inspect
    _params = _inspect.signature(fn).parameters
    _accepts_kwargs = any(p.kind == _inspect.Parameter.VAR_KEYWORD
                          for p in _params.values())

    async def _run(dispatcher, args, ctx):
        if _accepts_kwargs:
            return await fn(dispatcher, **args)
        filtered = {k: v for k, v in args.items() if k in _params}
        return await fn(dispatcher, **filtered)
    return _run


TOOL_REGISTRY = {}

_GROUP_SCOPED_TOOLS = {
    "get_group_info", "get_member_info", "get_recent_messages", "get_group_files",
    "get_file_url", "get_group_notice", "get_group_honor", "get_shut_list",
    "get_essence_list", "get_group_info_ex", "get_group_at_all_remain",
    "get_group_msg_history", "playful_ban",
}
_GLOBAL_OWNER_TOOLS = {"get_friend_list", "get_recent_contact"}
_TOOL_FEATURES = {
    "get_group_files": "file", "get_file_url": "file",
    "playful_ban": "management",
    "set_msg_emoji_like": "interaction", "send_like": "interaction",
    "send_music_card": "interaction",
}


def _feature_is_enabled(dispatcher, group_id, category, level):
    if not category or level >= 5:
        return True
    from bot.permission import get_group_config
    if group_id:
        config = get_group_config(dispatcher, group_id).get("ai_tools", {})
    else:
        config = dispatcher.config.get("ai_tools", {})
    return bool(config.get(category, False))


def _tool_visible(name, entry, *, explicit, actor_level=None, group_id=0,
                  dispatcher=None, bot_role="member"):
    if entry["tier"] != "read" and not explicit:
        return False
    if actor_level is None:
        return True
    if name in _GLOBAL_OWNER_TOOLS and actor_level < 5:
        return False
    if name in _GROUP_SCOPED_TOOLS and not group_id:
        return False
    if entry["tier"] == "playful":
        if actor_level < 2 or bot_role not in ("admin", "owner"):
            return False
    category = _TOOL_FEATURES.get(name)
    if dispatcher is not None and not _feature_is_enabled(
            dispatcher, group_id, category, actor_level):
        return False
    return True


def _register(name, handler, tier, description, parameters):
    TOOL_REGISTRY[name] = {"handler": handler, "tier": tier,
                           "description": description, "parameters": parameters}


_READ_TOOLS = [
    ("get_group_info", get_group_info, "查看本群资料（群名、人数等）",
     _schema({})),
    ("get_member_info", get_member_info, "查看群成员资料（昵称、群名片、角色、入群时间）",
     _schema({"user_id": _p_int("目标QQ号，不填则为当前说话的人")})),
    ("get_recent_messages", get_recent_messages, "查看本群最近消息（最多20条）",
     _schema({"count": _p_int("条数1-20，默认10")})),
    ("get_group_files", get_group_files, "查看群文件列表，可按关键词过滤",
     _schema({"keyword": _p_str("文件名关键词，可空")})),
    ("get_file_url", get_file_url, "获取群文件下载链接",
     _schema({"file_id": _p_str("文件ID"), "busid": _p_int("busid")},
             ("file_id", "busid"))),
    ("get_group_notice", get_group_notice, "查看群公告列表", _schema({})),
    ("get_group_honor", get_group_honor, "查看群荣誉（龙王、群聊之火等）",
     _schema({"honor_type": _p_str("all/talkative/performer/legend/strong_newbie/emotion，默认all")})),
    ("get_shut_list", get_shut_list, "查看当前被禁言的成员列表", _schema({})),
    ("get_friend_info", get_friend_info, "查看任意QQ号的资料卡片",
     _schema({"user_id": _p_int("目标QQ号")}, ("user_id",))),
    ("ocr_image", get_image_ocr, "识别图片里的文字",
     _schema({"image": _p_str("图片file id")}, ("image",))),
    ("get_essence_list", get_essence_list, "查看群精华消息列表", _schema({})),
    ("get_group_info_ex", get_group_info_ex, "查看本群更详细的资料", _schema({})),
    ("check_url_safely", check_url_safety, "检测链接是否安全",
     _schema({"url": _p_str("要检测的链接")}, ("url",))),
    ("translate_en2zh", translate_text, "英译中",
     _schema({"text": _p_str("要翻译的英文")}, ("text",))),
    ("get_group_at_all_remain", get_at_all_remain, "查询本群今天还能@全体几次", _schema({})),
    ("uapi_weather", uapi_weather, "查真实天气",
     _schema({"city": _p_str("城市名")}, ("city",))),
    ("uapi_hotboard", uapi_hotboard, "查各平台热榜",
     _schema({"type": _p_str("weibo/zhihu/bilibili/douyin/baidu/toutiao/ithome/github")})),
    ("uapi_saying", uapi_saying, "随机一句名言（一言）", _schema({})),
    ("uapi_answerbook", uapi_answerbook, "答案之书，给一个问题一个玄学回答",
     _schema({"question": _p_str("问题，可空")})),
    ("uapi_epic_free", uapi_epic_free, "查Epic本周免费游戏", _schema({})),
    ("get_group_msg_history", get_group_msg_history, "追溯本群聊天记录（最多50条，返回精简文本）",
     _schema({"count": _p_int("条数1-50，默认20")})),
    ("get_forward_msg", get_forward_msg, "查看合并转发消息的内容",
     _schema({"message_id": _p_int("合并转发的消息id")}, ("message_id",))),
    ("get_friend_list", get_friend_list, "查看机器人的好友列表", _schema({})),
    ("get_recent_contact", get_recent_contact, "查看最近联系过的会话",
     _schema({"count": _p_int("条数1-30，默认10")})),
    ("uapi_search", uapi_search, "联网搜索（聚合搜索结果）",
     _schema({"query": _p_str("搜索关键词")}, ("query",))),
    ("uapi_translate", uapi_translate, "多语言翻译（uapis通道）",
     _schema({"text": _p_str("要翻译的文本")}, ("text",))),
]

_INTERACTION_TOOLS = [
    ("set_msg_emoji_like", _tool_set_msg_emoji_like, "给某条消息贴表情回应",
     _schema({"message_id": _p_int("消息id，不填则为当前消息"),
              "emoji_id": _p_str("表情id，默认128077(点赞)")})),
    ("send_like", _tool_send_like, "给群友资料卡点赞",
     _schema({"user_id": _p_int("目标QQ号，不填则为当前说话的人"),
              "times": _p_int("次数1-10，默认1")})),
    ("send_music_card", _tool_send_music_card, "点歌：搜网易云歌曲并发音乐卡片到当前会话",
     _schema({"keyword": _p_str("歌名/歌手关键词")}, ("keyword",))),
]

_PLAYFUL_TOOLS = [
    ("playful_ban", execute_playful_ban,
     "玩闹禁言：只在互相调侃或本人自请的玩闹语境用，1-120秒，用完要说明是玩闹",
     _schema({"user_id": _p_int("目标QQ号"),
              "duration": _p_int("秒数1-120，默认30"),
              "reason": _p_str("玩闹理由，50字内")}, ("user_id",))),
]

for _name, _fn, _desc, _params in _READ_TOOLS:
    _register(_name, _wrap_read(_fn), "read", _desc, _params)
for _name, _fn, _desc, _params in _INTERACTION_TOOLS:
    _register(_name, _fn, "interaction", _desc, _params)
for _name, _fn, _desc, _params in _PLAYFUL_TOOLS:
    _register(_name, _fn, "playful", _desc, _params)


async def _tool_get_bot_help(dispatcher, args, ctx):
    """Self-knowledge tool: reuse the /help digest with caller-level filtering."""
    from bot.commands.system import build_help_digest
    status, _matched, text = build_help_digest(
        getattr(dispatcher, "commands", {}) or {},
        int(ctx.get("actor_level") or 0),
        str(args.get("command_or_category") or ""),
        group_id=int(ctx.get("group_id") or 0),
        bot_role=str(ctx.get("bot_role") or "member"),
    )
    if status != "ok":
        return {"ok": False, "error": "help_" + status,
                "message": "没有这个命令，或它不在你当前身份的菜单里"}
    return {"ok": True, "data": text}


_register(
    "get_bot_help", _tool_get_bot_help, "read",
    "查询小汐的功能和命令用法：留空返回按你身份过滤的功能分类概览，传入命令名或分类名返回详细用法",
    _schema({"command_or_category": _p_str("命令名或功能分类名，可空")}))


def build_tool_schemas(explicit=False, *, actor_level=None, group_id=0,
                       dispatcher=None, bot_role="member"):
    """Build only tools available to the verified actor and current scene."""
    tools = []
    for name, entry in TOOL_REGISTRY.items():
        if not _tool_visible(
                name, entry, explicit=explicit, actor_level=actor_level,
                group_id=int(group_id or 0), dispatcher=dispatcher,
                bot_role=bot_role):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": entry["description"],
                "parameters": entry["parameters"],
            },
        })
    return tools


async def execute_ai_tool(dispatcher, name, arguments, group_id=0, user_id=0,
                          message_id=0, interaction_allowed=False):
    """Execute a tool after real-time identity, feature and scope validation."""
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        return {"ok": False, "error": "tool_not_allowed", "tool": name}
    tier = entry.get("tier", "read")
    if tier != "read" and not interaction_allowed:
        return {"ok": False, "error": "tool_not_in_scene", "tool": name}

    from bot.permission import get_bot_role, get_user_level
    context_group = int(group_id or 0)
    context_user = int(user_id or 0)
    level, _ = await get_user_level(
        dispatcher, context_group or None, context_user, "member")
    bot_role = "member"
    if context_group and tier == "playful":
        bot_role, _ = await get_bot_role(dispatcher, context_group)
    if not _tool_visible(
            name, entry, explicit=interaction_allowed, actor_level=level,
            group_id=context_group, dispatcher=dispatcher, bot_role=bot_role):
        return {"ok": False, "error": "permission_denied", "tool": name}

    args = dict(arguments) if isinstance(arguments, dict) else {}
    ctx = {"group_id": context_group, "user_id": context_user,
           "message_id": int(message_id or 0), "actor_level": level,
           "bot_role": bot_role}
    if name in _GROUP_SCOPED_TOOLS:
        if not context_group:
            return {"ok": False, "error": "group_context_required", "tool": name}
        args["group_id"] = context_group
    elif "group_id" in args:
        args.pop("group_id", None)
    if name == "get_member_info" and not args.get("user_id"):
        args["user_id"] = context_user
    if tier == "interaction" and interaction_quota_left(context_group) <= 0:
        return {"ok": False, "error": "interaction_quota_exhausted", "tool": name}
    try:
        result = await entry["handler"](dispatcher, args, ctx)
    except Exception as exc:
        log.warning("AI tool %s failed: %s", name, exc)
        return {"ok": False, "error": "tool_failed", "tool": name,
                "message": _clip(exc, 200)}
    if tier == "interaction":
        if isinstance(result, dict) and result.get("ok"):
            _record_interaction_usage(context_group)
        log.info("INTERACTION_TOOL group=%s user=%s tool=%s", context_group,
                 context_user, name)
    if isinstance(result, dict):
        result.setdefault("tool", name)
    return result
