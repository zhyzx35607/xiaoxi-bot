"""Human-controlled Agent controls; no AI path can call these implicitly."""

import re
import time

from ..permission import LEVEL_GOWNER, LEVEL_SUPER, get_group_config, get_user_level, save_group_config


def _scope_key(group_id, user_id):
    return "group:{}".format(group_id) if group_id else "owner:{}".format(user_id)


def _duration_seconds(text):
    match = re.fullmatch(r"(\d+)(\u79d2|\u5206\u949f|\u5206|\u5c0f\u65f6|\u65f6|\u5929)", str(text).strip())
    if not match:
        return None
    factors = {"\u79d2": 1, "\u5206\u949f": 60, "\u5206": 60, "\u5c0f\u65f6": 3600, "\u65f6": 3600, "\u5929": 86400}
    return max(10, min(int(match.group(1)) * factors[match.group(2)], 365 * 86400))


async def _goal_command(dispatcher, group_id, user_id, rest):
    scope = _scope_key(group_id, user_id)
    parts = rest.split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    value = parts[1].strip() if len(parts) > 1 else ""
    if action in {"add", "\u6dfb\u52a0", "\u65b0\u5efa"}:
        if not value:
            await dispatcher._reply(group_id, user_id, "\u7528\u6cd5\uff1a/agent \u76ee\u6807 add \u76ee\u6807\u5185\u5bb9")
            return
        goal = dispatcher.agent_runtime.goals.create(scope, user_id, value)
        await dispatcher._reply(group_id, user_id, "\u5df2\u5efa\u7acb\u76ee\u6807 {}\uff1a{}".format(goal["id"], goal["title"]))
        return
    if action in {"done", "\u5b8c\u6210", "cancel", "\u53d6\u6d88"}:
        if not value:
            await dispatcher._reply(group_id, user_id, "\u8bf7\u63d0\u4f9b\u76ee\u6807 ID")
            return
        status = "done" if action in {"done", "\u5b8c\u6210"} else "cancelled"
        goal = dispatcher.agent_runtime.goals.update(scope, value, status=status)
        text = "\u76ee\u6807\u672a\u627e\u5230" if not goal else ("\u76ee\u6807\u5df2\u5b8c\u6210" if status == "done" else "\u76ee\u6807\u5df2\u53d6\u6d88")
        await dispatcher._reply(group_id, user_id, text)
        return
    goals = dispatcher.agent_runtime.goals.list(scope)
    if not goals:
        await dispatcher._reply(group_id, user_id, "\u5f53\u524d\u6ca1\u6709\u8fdb\u884c\u4e2d\u7684\u76ee\u6807")
        return
    lines = ["Agent \u76ee\u6807\uff1a"]
    lines.extend("{} [{}] {}{}".format(item["id"], item["status"], item["title"], " -- " + item["progress"] if item.get("progress") else "") for item in goals[:20])
    await dispatcher._reply(group_id, user_id, "\n".join(lines))


async def _reminder_command(dispatcher, group_id, user_id, rest):
    scope = _scope_key(group_id, user_id)
    parts = rest.split(maxsplit=2)
    action = parts[0].lower() if parts else "list"
    if action in {"add", "\u6dfb\u52a0", "\u65b0\u5efa"}:
        if len(parts) < 3:
            await dispatcher._reply(group_id, user_id, "\u7528\u6cd5\uff1a/agent \u63d0\u9192 add 30\u5206\u949f \u63d0\u9192\u5185\u5bb9")
            return
        seconds = _duration_seconds(parts[1])
        if seconds is None:
            await dispatcher._reply(group_id, user_id, "\u65f6\u95f4\u683c\u5f0f\u793a\u4f8b\uff1a30\u5206\u949f\u30012\u5c0f\u65f6\u30011\u5929")
            return
        reminder = dispatcher.agent_runtime.reminders.create(scope, user_id, parts[2], time.time() + seconds)
        await dispatcher._reply(group_id, user_id, "\u63d0\u9192\u5df2\u5efa\u7acb {}\uff0c\u7ea6 {} \u540e\u89e6\u53d1".format(reminder["id"], parts[1]))
        return
    if action in {"cancel", "\u53d6\u6d88"}:
        reminder_id = parts[1] if len(parts) > 1 else ""
        result = dispatcher.agent_runtime.reminders.cancel(scope, reminder_id)
        await dispatcher._reply(group_id, user_id, "\u63d0\u9192\u5df2\u53d6\u6d88" if result else "\u6ca1\u6709\u627e\u5230\u8fd9\u4e2a\u63d0\u9192")
        return
    reminders = dispatcher.agent_runtime.reminders.list(scope)
    if not reminders:
        await dispatcher._reply(group_id, user_id, "\u5f53\u524d\u6ca1\u6709\u5f85\u89e6\u53d1\u63d0\u9192")
        return
    lines = ["Agent \u63d0\u9192\uff1a"]
    lines.extend("{} {} {}".format(item["id"], time.strftime("%m-%d %H:%M", time.localtime(item["due_at"])), item["text"]) for item in reminders[:20])
    await dispatcher._reply(group_id, user_id, "\n".join(lines))


async def _memory_command(dispatcher, group_id, user_id, rest):
    scope = _scope_key(group_id, user_id)
    parts = rest.split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    if action in {"confirm", "\u786e\u8ba4"}:
        try:
            index = int(parts[1]) - 1
        except (IndexError, ValueError):
            await dispatcher._reply(group_id, user_id, "\u7528\u6cd5\uff1a/agent \u8bb0\u5fc6 confirm \u5e8f\u53f7")
            return
        text = "\u8bb0\u5fc6\u5df2\u786e\u8ba4" if dispatcher.agent_runtime.memory.confirm(scope, index) else "\u6ca1\u6709\u8fd9\u4e2a\u5f85\u786e\u8ba4\u8bb0\u5fc6"
        await dispatcher._reply(group_id, user_id, text)
        return
    pending = dispatcher.agent_runtime.memory.list_records(scope, confirmed=False)
    confirmed = dispatcher.agent_runtime.memory.list_records(scope, confirmed=True)
    lines = ["\u5df2\u786e\u8ba4\u8bb0\u5fc6\uff1a{} \u6761\uff1b\u5f85\u786e\u8ba4\uff1a{} \u6761".format(len(confirmed), len(pending))]
    lines.extend("{}. {}".format(index + 1, item.get("content", "")[:100]) for index, item in enumerate(pending[:20]))
    await dispatcher._reply(group_id, user_id, "\n".join(lines))


async def _task_command(dispatcher, group_id, user_id, rest):
    scope = _scope_key(group_id, user_id)
    parts = rest.split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    if action in {"cancel", "\u53d6\u6d88"}:
        task_id = parts[1].strip() if len(parts) > 1 else ""
        item = next((task for task in dispatcher.agent_runtime.tasks.list(scope) if task.get("id") == task_id), None)
        if not item or item.get("status") not in {"queued", "running"}:
            await dispatcher._reply(group_id, user_id, "\u6ca1\u6709\u627e\u5230\u53ef\u53d6\u6d88\u7684\u540e\u53f0\u4efb\u52a1")
            return
        dispatcher.agent_runtime.tasks.update(task_id, "cancelled")
        await dispatcher._reply(group_id, user_id, "\u540e\u53f0\u4efb\u52a1\u5df2\u53d6\u6d88")
        return
    tasks = dispatcher.agent_runtime.tasks.list(scope)
    if not tasks:
        await dispatcher._reply(group_id, user_id, "\u5f53\u524d\u6ca1\u6709\u540e\u53f0\u4efb\u52a1")
        return
    lines = ["Agent \u540e\u53f0\u4efb\u52a1\uff1a"]
    lines.extend("{} [{}] {}".format(item.get("id"), item.get("status"), item.get("goal", "")[:120]) for item in tasks[-20:])
    await dispatcher._reply(group_id, user_id, "\n".join(lines))


async def cmd_agent(dispatcher, group_id, user_id, args, role, sender_card, message):
    parts = args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "status"
    value = parts[1].strip() if len(parts) > 1 else ""
    level, _ = await get_user_level(dispatcher, group_id, user_id, role)
    if action in {"on", "off"} and level < LEVEL_SUPER:
        await dispatcher._reply(group_id, user_id, "Agent \u5168\u5c40\u5f00\u5173\u53ea\u6709\u6700\u9ad8\u4e3b\u4eba\u80fd\u6539")
        return
    if action in {"on", "off"}:
        enabled = action == "on"
        settings = dispatcher.config.setdefault("agent", {})
        settings["enabled"] = enabled
        settings["observation_only"] = not enabled
        settings["primary_router"] = enabled
        settings["owner_autonomy_enabled"] = enabled
        settings["worker_enabled"] = True
        save_group_config(dispatcher)
        await dispatcher._reply(group_id, user_id, "Agent \u89c4\u5212\u4e3b\u8def\u7531\u5df2{}".format("\u5f00\u542f" if enabled else "\u5173\u95ed\uff0c\u5df2\u56de\u5230\u65e7\u7248 AI \u8def\u7531"))
        return
    if action in {"\u81ea\u6cbb", "autonomy"}:
        if level < LEVEL_SUPER:
            await dispatcher._reply(group_id, user_id, "\u6700\u9ad8\u4e3b\u4eba\u81ea\u6cbb\u6a21\u5f0f\u53ea\u6709\u6700\u9ad8\u4e3b\u4eba\u80fd\u8bbe\u7f6e")
            return
        enabled = value.lower() in {"on", "\u5f00\u542f", "1", "true"}
        settings = dispatcher.config.setdefault("agent", {})
        settings["owner_autonomy_enabled"] = enabled
        settings["worker_enabled"] = True
        settings["primary_router"] = enabled or settings.get("primary_router", False)
        settings["observation_only"] = not enabled
        save_group_config(dispatcher)
        await dispatcher._reply(group_id, user_id, "\u6700\u9ad8\u4e3b\u4eba\u79c1\u57df\u81ea\u6cbb\u5df2{}".format("\u5f00\u542f" if enabled else "\u5173\u95ed"))
        return
    if action in {"\u4e3b\u52a8", "proactive"}:
        if level < LEVEL_GOWNER or not group_id:
            await dispatcher._reply(group_id, user_id, "\u7fa4\u57df\u4e3b\u52a8 Agent \u53ea\u80fd\u7531\u6700\u9ad8\u4e3b\u4eba\u6216\u5f53\u524d\u7fa4\u4e3b\u5728\u7fa4\u91cc\u8bbe\u7f6e")
            return
        enabled = value.lower() in {"on", "\u5f00\u542f", "1", "true"}
        group_agent = get_group_config(dispatcher, group_id).setdefault("agent", {})
        group_agent["proactive_enabled"] = enabled
        group_agent["primary_router"] = enabled
        save_group_config(dispatcher)
        await dispatcher._reply(group_id, user_id, "\u672c\u7fa4\u4e3b\u52a8 Agent \u5df2{}".format("\u5f00\u542f" if enabled else "\u5173\u95ed"))
        return
    if action in {"\u76ee\u6807", "goal", "goals"}:
        await _goal_command(dispatcher, group_id, user_id, value)
        return
    if action in {"\u63d0\u9192", "reminder", "remind"}:
        await _reminder_command(dispatcher, group_id, user_id, value)
        return
    if action in {"\u8bb0\u5fc6", "memory"}:
        await _memory_command(dispatcher, group_id, user_id, value)
        return
    if action in {"\u4efb\u52a1", "task", "tasks"}:
        await _task_command(dispatcher, group_id, user_id, value)
        return
    scope = _scope_key(group_id, user_id)
    pending = dispatcher.agent_runtime.memory.list_records(scope, confirmed=False)
    goals = dispatcher.agent_runtime.goals.list(scope)
    reminders = dispatcher.agent_runtime.reminders.list(scope)
    tasks = dispatcher.agent_runtime.tasks.list(scope, statuses={"queued", "running"})
    settings = dispatcher.config.get("agent", {})
    lines = [
        "Agent \u72b6\u6001\uff1a{}".format("\u5f00\u542f" if settings.get("enabled", True) else "\u5173\u95ed"),
        "\u8fd0\u884c\u9636\u6bb5\uff1a{}".format("\u89c2\u5bdf\u6a21\u5f0f" if settings.get("observation_only", True) else "\u81ea\u6cbb\u89c4\u5212\u6a21\u5f0f"),
        "\u76ee\u6807\uff1a{}\uff1b\u63d0\u9192\uff1a{}\uff1b\u540e\u53f0\u4efb\u52a1\uff1a{}\uff1b\u5f85\u786e\u8ba4\u8bb0\u5fc6\uff1a{}".format(len(goals), len(reminders), len(tasks), len(pending)),
        "\u547d\u4ee4\uff1a/agent \u76ee\u6807 | \u63d0\u9192 | \u8bb0\u5fc6 | \u4efb\u52a1 | \u81ea\u6cbb on/off | \u4e3b\u52a8 on/off",
    ]
    await dispatcher._reply(group_id, user_id, "\n".join(lines))
