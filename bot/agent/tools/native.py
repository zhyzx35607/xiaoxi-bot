"""Agent-native tools bound to the authenticated event scope."""

import time


NATIVE_TOOL_DESCRIPTIONS = {
    "agent_create_goal": "创建当前作用域长期目标；参数 title",
    "agent_update_goal": "更新当前作用域目标；参数 goal_id，可选 status/progress",
    "agent_list_goals": "列出当前作用域进行中目标；无参数",
    "agent_create_reminder": "创建当前作用域提醒；参数 text、delay_seconds(10到31536000)",
    "agent_list_reminders": "列出当前作用域待触发提醒；无参数",
    "agent_list_tasks": "列出当前作用域后台任务；无参数",
}


async def execute_native(runtime, agent_event, tool_name, arguments):
    scope_key = agent_event.scope.key
    user_id = agent_event.identity.user_id
    if tool_name == "agent_create_goal":
        title = str(arguments.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "missing_title"}
        return {"ok": True, "data": runtime.goals.create(scope_key, user_id, title)}
    if tool_name == "agent_update_goal":
        goal_id = str(arguments.get("goal_id") or "").strip()
        if not goal_id:
            return {"ok": False, "error": "missing_goal_id"}
        item = runtime.goals.update(
            scope_key, goal_id,
            status=str(arguments.get("status") or "")[:30] or None,
            progress=arguments.get("progress") if "progress" in arguments else None,
        )
        return {"ok": bool(item), "data": item, "error": "goal_not_found" if not item else ""}
    if tool_name == "agent_list_goals":
        return {"ok": True, "data": runtime.goals.list(scope_key)[:20]}
    if tool_name == "agent_create_reminder":
        text = str(arguments.get("text") or "").strip()
        try:
            delay = int(arguments.get("delay_seconds"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_delay_seconds"}
        if not text or delay < 10 or delay > 365 * 86400:
            return {"ok": False, "error": "invalid_reminder"}
        reminder = runtime.reminders.create(scope_key, user_id, text, time.time() + delay)
        return {"ok": True, "data": reminder}
    if tool_name == "agent_list_reminders":
        return {"ok": True, "data": runtime.reminders.list(scope_key)[:20]}
    if tool_name == "agent_list_tasks":
        return {"ok": True, "data": runtime.tasks.list(scope_key)[:20]}
    return {"ok": False, "error": "unknown_native_tool"}
