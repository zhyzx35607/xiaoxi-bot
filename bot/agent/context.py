"""Build bounded, scope-isolated context for the Agent planner."""

import time


class AgentContextBuilder:
    def __init__(self, runtime):
        self.runtime = runtime

    def build(self, agent_event):
        scope = agent_event.scope.key
        confirmed = self.runtime.memory.list_records(scope, confirmed=True)[-12:]
        goals = self.runtime.goals.list(scope)[:12]
        reminders = self.runtime.reminders.list(scope)[:12]
        events = self.runtime.store.read(
            "events/{}.json".format(scope.replace(":", "_")), [])
        if not isinstance(events, list):
            events = []
        lines = ["当前时间戳：{}".format(int(time.time())), "作用域：{}".format(scope)]
        if confirmed:
            lines.append("已确认长期记忆：")
            lines.extend("- {}".format(item.get("content", "")[:200]) for item in confirmed)
        if goals:
            lines.append("进行中目标：")
            lines.extend("- {} [{}] {}".format(item.get("id"), item.get("status"), item.get("title", "")[:200]) for item in goals)
        if reminders:
            lines.append("待触发提醒：")
            lines.extend("- {} @{} {}".format(item.get("id"), int(item.get("due_at", 0)), item.get("text", "")[:200]) for item in reminders)
        recent = [item for item in events[-12:] if item.get("event_id") != agent_event.event_id]
        if recent:
            lines.append("最近事件：")
            lines.extend("- 用户{}：{}".format(item.get("user_id"), item.get("text", "")[:200]) for item in recent)
        return "\n".join(lines)[:7000]
