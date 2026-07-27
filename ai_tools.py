"""Small, safe AI-facing tool layer for NapCat capabilities."""

import json
import logging

log = logging.getLogger("qqbot")


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
    if not uapi.credits_available(dispatcher.config, "user"):
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
    if not uapi.credits_available(dispatcher.config, "user"):
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
    if not uapi.credits_available(dispatcher.config, "user"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/saying/random", kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {
        "content": data.get("content"), "author": data.get("author"),
        "source": data.get("source"),
    }}


async def uapi_answerbook(dispatcher, question=""):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user"):
        return {"ok": False, "error": "credit_budget_exhausted"}
    data = await uapi.uapi_get(dispatcher, "/answerbook/ask",
                               params={"question": str(question)[:60] or "今天会发生什么"},
                               kind="user")
    if not data:
        return {"ok": False, "error": "uapi_failed"}
    return {"ok": True, "data": {"answer": data.get("answer")}}


async def uapi_epic_free(dispatcher):
    from bot import uapi
    if not uapi.credits_available(dispatcher.config, "user"):
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
    }
    handler = tools.get(name)
    if not handler:
        return {"ok": False, "error": "tool_not_allowed", "tool": name}
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
    key = _quota_key(group_id)
    if interaction_quota_left(group_id) <= 0:
        return {"ok": False, "error": "interaction_quota_exhausted", "tool": name}
    try:
        result = await handler()
    except Exception as exc:
        log.warning("interaction tool %s failed: %s", name, exc)
        return {"ok": False, "error": "tool_failed", "tool": name,
                "message": _clip(exc, 200)}
    _interaction_usage[key] = _interaction_usage.get(key, 0) + 1
    if len(_interaction_usage) > 500:
        today = _time.strftime("%Y%m%d")
        for item in list(_interaction_usage):
            if not item.startswith(today):
                _interaction_usage.pop(item, None)
    log.info("INTERACTION_TOOL group=%s user=%s tool=%s status=%s",
             group_id, user_id, name, result.get("status"))
    return {"ok": result.get("status") == "ok", "tool": name,
            "message": result.get("msg") or result.get("wording", "")}


def reset_quota_for_test():
    _interaction_usage.clear()


def format_tool_result(result):
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:1800]
