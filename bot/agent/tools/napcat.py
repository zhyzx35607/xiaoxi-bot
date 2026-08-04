"""Explicitly allowlisted NapCat read capabilities for the Agent."""

SAFE_ACTIONS = {
    "get_group_ignore_add_request": {},
    "get_group_ignored_notifies": {},
    "get_online_clients": {},
    "nc_get_user_status": {"user_id": int},
    "get_mini_app_ark": {"type": str, "title": str, "prompt": str, "meta": str},
}


async def napcat_read(dispatcher, tool_name, **params):
    schema = SAFE_ACTIONS.get(tool_name)
    if schema is None:
        return {"ok": False, "error": "napcat_action_not_allowlisted"}
    normalized = {}
    for key, converter in schema.items():
        if key in params and params[key] is not None:
            try:
                normalized[key] = converter(params[key])
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid_tool_argument", "argument": key}
    result = await dispatcher.client.call(tool_name, normalized)
    return {"ok": result.get("status") == "ok", "data": result.get("data"), "message": result.get("msg") or result.get("wording", "")}
