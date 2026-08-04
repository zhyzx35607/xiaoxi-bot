"""Agent-native tools bound to the authenticated event scope."""

import time


NATIVE_TOOL_DESCRIPTIONS = {
    "agent_create_goal": "创建当前作用域长期目标；参数 title",
    "agent_update_goal": "更新当前作用域目标；参数 goal_id，可选 status/progress",
    "agent_list_goals": "列出当前作用域进行中目标；无参数",
    "agent_create_reminder": "创建当前作用域提醒；参数 text、delay_seconds(10到31536000)",
    "agent_list_reminders": "列出当前作用域待触发提醒；无参数",
    "agent_list_tasks": "列出当前作用域后台任务；无参数",
    "agent_create_plan": "创建当前作用域多步计划；参数 title、steps、success_criteria",
    "agent_update_plan_step": "更新当前作用域计划步骤；参数 plan_id、step_id、status、evidence、result",
    "agent_list_plans": "列出当前作用域计划；无参数",
    "agent_add_insight": "沉淀当前作用域有证据的洞察；参数 content、category、confidence、evidence",
    "agent_list_insights": "列出当前作用域洞察；无参数",
    "agent_list_timeline": "列出当前作用域近期行动时间线；可选 limit",
    "agent_create_skill": "创建当前作用域技能/SOP；参数 name、instructions、triggers",
    "agent_list_skills": "列出当前作用域技能/SOP；无参数",
}


WRITE_TOOLS = {
    "agent_create_goal", "agent_update_goal", "agent_create_reminder",
    "agent_create_plan", "agent_update_plan_step", "agent_add_insight",
    "agent_create_skill",
}


async def execute_native(runtime, agent_event, tool_name, arguments):
    scope_key = agent_event.scope.key
    user_id = agent_event.identity.user_id
    if tool_name in WRITE_TOOLS and not agent_event.identity.can_manage_agent:
        return {"ok": False, "error": "agent_write_requires_owner"}
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
    if tool_name == "agent_create_plan":
        title = str(arguments.get("title") or "").strip()
        steps = arguments.get("steps") if isinstance(arguments.get("steps"), list) else []
        if not title or not steps:
            return {"ok": False, "error": "invalid_plan"}
        item = runtime.plans.create(
            scope_key, user_id, title, steps,
            success_criteria=str(arguments.get("success_criteria") or ""),
            source_event_id=agent_event.event_id,
        )
        runtime.timeline.add(scope_key, "plan_created", title, actor_id=user_id, metadata={"plan_id": item["id"]})
        return {"ok": True, "data": item}
    if tool_name == "agent_update_plan_step":
        plan_id = str(arguments.get("plan_id") or "").strip()
        step_id = str(arguments.get("step_id") or "").strip()
        status = str(arguments.get("status") or "").strip()
        if not plan_id or not step_id or status not in {"pending", "running", "done", "failed", "cancelled", "skipped"}:
            return {"ok": False, "error": "invalid_plan_step_update"}
        item = runtime.plans.update_step(
            scope_key, plan_id, step_id, status,
            evidence=arguments.get("evidence", ""), result=arguments.get("result", ""))
        return {"ok": bool(item), "data": item, "error": "plan_or_step_not_found" if not item else ""}
    if tool_name == "agent_list_plans":
        return {"ok": True, "data": runtime.plans.list(scope_key)[:20]}
    if tool_name == "agent_add_insight":
        content = str(arguments.get("content") or "").strip()
        evidence = str(arguments.get("evidence") or "").strip()
        if not content or not evidence:
            return {"ok": False, "error": "insight_requires_evidence"}
        try:
            confidence = float(arguments.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        item = runtime.insights.add(
            scope_key, content, category=arguments.get("category", "reflection"),
            confidence=confidence, evidence=evidence, source_id=agent_event.event_id)
        return {"ok": bool(item), "data": item}
    if tool_name == "agent_list_insights":
        return {"ok": True, "data": runtime.insights.list(scope_key, limit=20)}
    if tool_name == "agent_list_timeline":
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        return {"ok": True, "data": runtime.timeline.list(scope_key, limit=limit)}
    if tool_name == "agent_create_skill":
        name = str(arguments.get("name") or "").strip()
        instructions = str(arguments.get("instructions") or "").strip()
        triggers = arguments.get("triggers") if isinstance(arguments.get("triggers"), list) else []
        if not name or not instructions:
            return {"ok": False, "error": "invalid_skill"}
        return {"ok": True, "data": runtime.skills.create(scope_key, user_id, name, instructions, triggers=triggers)}
    if tool_name == "agent_list_skills":
        return {"ok": True, "data": runtime.skills.list(scope_key)[:20]}
    return {"ok": False, "error": "unknown_native_tool"}
