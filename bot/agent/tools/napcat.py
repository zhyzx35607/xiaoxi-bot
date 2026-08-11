"""Registry-backed, scope-bound NapCat read capabilities for the Agent."""

import inspect

from api_registry import REGISTRY
from ...memory import sanitize_for_memory


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
