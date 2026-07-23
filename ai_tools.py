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


def format_tool_result(result):
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:1800]
