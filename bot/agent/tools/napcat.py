"""Registry-backed, scope-bound NapCat read capabilities for the Agent."""

import inspect

from api_registry import REGISTRY
from ...memory import sanitize_for_memory
from ...permission import can_moderate_target, get_bot_role
from ...utils import now_in_timezone


EXPLICIT_SAFE_ACTIONS = {
    "get_group_ignore_add_request": {},
    "get_group_ignored_notifies": {},
    "get_online_clients": {},
    "nc_get_user_status": {"user_id": int},
    "get_mini_app_ark": {"type": str, "title": str, "prompt": str, "meta": str},
}

REGISTRY_SAFE_ACTIONS = {
    name: spec for name, spec in REGISTRY.items()
    if spec.ai_allowed and spec.risk == "read"
}

SAFE_ACTIONS = {**{name: {} for name in REGISTRY_SAFE_ACTIONS}, **EXPLICIT_SAFE_ACTIONS}

FORBIDDEN_ARGUMENTS = {
    "cookie", "cookies", "token", "access_token", "authorization", "client_key",
    "csrf", "bkn", "rkey", "credential", "credentials",
}

OWNER_PRIVATE_ONLY_ACTIONS = {
    "get_group_ignore_add_request", "get_group_ignored_notifies", "get_online_clients",
    "get_group_list", "get_friend_msg_history", "get_msg", "get_forward_msg",
    "get_image", "get_record", "get_file", "get_private_file_url",
    "get_robot_uin_range", "get_profile_like",
}


def action_description(name):
    spec = REGISTRY_SAFE_ACTIONS.get(name)
    if spec:
        return "NapCat 只读能力（{}，作用域 {}）".format(spec.category, spec.scope)
    return "NapCat 显式白名单只读能力"


# 受控群管层：不进 SAFE_ACTIONS，避免改变只读目录与 is_read_only 的现有语义。
# 低风险动作门控全部通过后可自动执行；高风险动作只允许经确认流（confirmed）
# 或最高主人触发。
LOW_RISK_MODERATION = {"delete_msg", "set_group_ban", "set_group_add_request"}
HIGH_RISK_MODERATION = {"set_group_kick"}
MODERATION_ACTIONS = LOW_RISK_MODERATION | HIGH_RISK_MODERATION

MODERATION_TOOL_DESCRIPTIONS = {
    "delete_msg": "撤回本群消息（低风险群管，需本群开启管群自治）；参数 message_id，可选 reason",
    "set_group_ban": "禁言本群成员（低风险群管，时长有硬上限）；参数 user_id、duration（秒），可选 reason",
    "set_group_add_request": "处理入群申请（低风险群管）；参数 flag、approve（布尔），可选 reason",
    "set_group_kick": "移出本群成员（高风险，需主人确认后才会执行）；参数 user_id，可选 reject_add_request、reason",
}


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _moderation_group_id(agent_event, params):
    if agent_event.scope.is_private:
        if not agent_event.identity.is_super_owner:
            return None, "moderation_group_scope_required"
        group_id = _to_int(params.get("group_id"))
        if group_id is None:
            return None, "group_id_required_for_owner_private"
        return group_id, ""
    return int(agent_event.scope.group_id), ""


def _moderation_quota_ok(runtime, group_id, limit, today):
    state = runtime.store.read("moderation/group_{}.json".format(group_id), {})
    if not isinstance(state, dict) or state.get("date") != today:
        return True
    return int(state.get("count", 0) or 0) < limit


def _record_moderation_quota(runtime, group_id, today):
    def change(state):
        if not isinstance(state, dict) or state.get("date") != today:
            state = {"date": today, "count": 0}
        state["count"] = int(state.get("count", 0) or 0) + 1
        return state, state["count"]

    return runtime.store.update("moderation/group_{}.json".format(group_id), {}, change)


async def napcat_moderation(dispatcher, agent_event, tool_name, **params):
    """Execute one gated moderation action; every gate fails closed."""
    if tool_name not in MODERATION_ACTIONS:
        return {"ok": False, "error": "moderation_action_not_allowlisted"}
    group_id, error = _moderation_group_id(agent_event, params)
    if group_id is None:
        return {"ok": False, "error": error}
    group = dispatcher.config.get("groups", {}).get(str(group_id), {})
    group_agent = group.get("agent", {}) if isinstance(group, dict) else {}
    if not group_agent.get("moderation_enabled", False):
        return {"ok": False, "error": "moderation_disabled"}
    bot_role, _ = await get_bot_role(dispatcher, group_id)
    if bot_role not in ("owner", "admin"):
        return {"ok": False, "error": "bot_not_group_admin"}
    if tool_name in HIGH_RISK_MODERATION:
        # 巡检是系统事件：主人身份豁免不适用，高风险动作只能走人工确认流。
        if agent_event.metadata.get("auto_patrol"):
            return {"ok": False, "error": "moderation_patrol_high_risk_forbidden"}
        confirmed = bool(agent_event.metadata.get("confirmed"))
        if not confirmed and not agent_event.identity.is_super_owner:
            return {"ok": False, "error": "moderation_requires_confirmation"}
    reason = str(params.get("reason") or "")[:200]
    call = None
    target_id = 0
    if tool_name == "delete_msg":
        message_id = _to_int(params.get("message_id"))
        if message_id is None:
            return {"ok": False, "error": "invalid_tool_argument", "argument": "message_id"}
        call = (dispatcher.client.delete_msg, (message_id,), {})
    elif tool_name == "set_group_ban":
        target_id = _to_int(params.get("user_id"))
        duration = _to_int(params.get("duration"))
        if target_id is None or duration is None:
            return {"ok": False, "error": "invalid_tool_argument", "argument": "user_id/duration"}
        ban_max = max(60, int(dispatcher.config.get("agent", {}).get(
            "moderation_ban_max_seconds", 600)))
        duration = max(0, min(duration, ban_max))
        call = (dispatcher.client.set_group_ban, (group_id, target_id, duration), {})
    elif tool_name == "set_group_add_request":
        flag = str(params.get("flag") or "").strip()
        approve = params.get("approve")
        if not flag or not isinstance(approve, bool):
            return {"ok": False, "error": "invalid_tool_argument", "argument": "flag/approve"}
        call = (dispatcher.client.set_group_add_request, (flag, "add", approve, reason), {})
    else:  # set_group_kick
        target_id = _to_int(params.get("user_id"))
        if target_id is None:
            return {"ok": False, "error": "invalid_tool_argument", "argument": "user_id"}
        call = (dispatcher.client.set_group_kick, (group_id, target_id),
                {"reject_add": bool(params.get("reject_add_request", False))})
    if target_id:
        allowed, deny_reason = await can_moderate_target(
            dispatcher, group_id, agent_event.identity.user_id, target_id,
            agent_event.identity.role)
        if not allowed:
            return {"ok": False, "error": "moderation_target_protected", "message": deny_reason}
    runtime = getattr(dispatcher, "agent_runtime", None)
    if runtime is None:
        return {"ok": False, "error": "agent_runtime_unavailable"}
    limit = max(1, int(dispatcher.config.get("agent", {}).get("moderation_daily_limit", 20)))
    today = now_in_timezone(dispatcher.config).strftime("%Y-%m-%d")
    if not _moderation_quota_ok(runtime, group_id, limit, today):
        return {"ok": False, "error": "moderation_quota_exceeded"}
    method, args, kwargs = call
    result = await method(*args, **kwargs)
    ok = isinstance(result, dict) and result.get("status") in {None, "ok"}
    if ok:
        _record_moderation_quota(runtime, group_id, today)
        runtime.timeline.add(
            "group:{}".format(group_id), "moderation",
            "{} 目标{} {}".format(tool_name, target_id or params.get("message_id") or params.get("flag", ""), reason),
            actor_id=agent_event.identity.user_id,
            metadata={"action": tool_name, "group_id": group_id,
                      "target": target_id, "reason": reason})
    if not isinstance(result, dict):
        return {"ok": True, "data": result}
    return {
        "ok": ok,
        "data": result.get("data", result),
        "message": result.get("message") or result.get("msg") or result.get("wording", ""),
    }


def _sanitize(value, depth=0):
    if depth > 3:
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:2000] if isinstance(value, str) else value
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _sanitize(item, depth + 1)
            for key, item in list(value.items())[:50]
            if str(key).lower() not in FORBIDDEN_ARGUMENTS
        }
    return str(value)[:500]


def _bound_arguments(agent_event, method, params):
    signature = inspect.signature(method)
    allowed = {
        name for name, parameter in signature.parameters.items()
        if name != "self" and parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY
        }
    }
    normalized = {
        key: _sanitize(value) for key, value in params.items()
        if key in allowed and key.lower() not in FORBIDDEN_ARGUMENTS
    }
    if "group_id" in allowed:
        if agent_event.scope.is_private:
            if not agent_event.identity.is_super_owner or "group_id" not in normalized:
                raise PermissionError("group_id_required_for_owner_private")
        else:
            normalized["group_id"] = int(agent_event.scope.group_id)
    if "user_id" in allowed and "user_id" not in normalized:
        normalized["user_id"] = int(agent_event.identity.user_id)
    missing = [
        name for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        and parameter.default is inspect.Parameter.empty
        and name not in normalized
    ]
    if missing:
        raise ValueError("missing required arguments: {}".format(", ".join(missing)))
    return normalized


async def napcat_read(dispatcher, agent_event, tool_name, **params):
    if tool_name in OWNER_PRIVATE_ONLY_ACTIONS:
        if not agent_event.scope.is_private or not agent_event.identity.is_super_owner:
            return {"ok": False, "error": "owner_private_tool_only"}
    if tool_name in EXPLICIT_SAFE_ACTIONS:
        schema = EXPLICIT_SAFE_ACTIONS[tool_name]
        normalized = {}
        for key, converter in schema.items():
            if key in params and params[key] is not None:
                try:
                    normalized[key] = converter(params[key])
                except (TypeError, ValueError):
                    return {"ok": False, "error": "invalid_tool_argument", "argument": key}
        result = await dispatcher.client.call(tool_name, normalized)
        return {"ok": result.get("status") == "ok", "data": result.get("data"), "message": result.get("msg") or result.get("wording", "")}
    spec = REGISTRY_SAFE_ACTIONS.get(tool_name)
    if not spec:
        return {"ok": False, "error": "napcat_action_not_allowlisted"}
    if spec.scope == "private" and not agent_event.scope.is_private:
        return {"ok": False, "error": "private_tool_outside_private_scope"}
    method = getattr(dispatcher.client, tool_name, None)
    if not callable(method):
        return {"ok": False, "error": "napcat_client_method_unavailable"}
    try:
        normalized = _bound_arguments(agent_event, method, params)
    except PermissionError as error:
        return {"ok": False, "error": str(error)}
    except ValueError as error:
        return {
            "ok": False,
            "error": "invalid_tool_arguments",
            "message": sanitize_for_memory(error)[:300],
        }
    try:
        result = method(**normalized)
        if inspect.isawaitable(result):
            result = await result
    except TypeError as error:
        return {
            "ok": False,
            "error": "invalid_tool_arguments",
            "message": sanitize_for_memory(error)[:300],
        }
    if not isinstance(result, dict):
        return {"ok": True, "data": result}
    return {
        "ok": result.get("status") in {None, "ok"},
        "data": result.get("data", result),
        "message": result.get("message") or result.get("msg") or result.get("wording", ""),
    }
