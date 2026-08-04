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
        active_plans = self.runtime.plans.list(scope, statuses={"active", "running"})[:5]
        insights = self.runtime.insights.list(scope, limit=8)
        profile = self.runtime.profiles.get(scope)
        skills = self.runtime.skills.match(scope, agent_event.text, limit=5)
        events = self.runtime.store.read(
            "events/{}.json".format(scope.replace(":", "_")), [])
        if not isinstance(events, list):
            events = []
        lines = ["当前时间戳：{}".format(int(time.time())), "作用域：{}".format(scope)]
        if profile:
            lines.append("当前作用域画像：")
            if profile.get("persona"):
                lines.append("- 身份与语气：{}".format(profile["persona"][:600]))
            if profile.get("customs"):
                lines.append("- 习惯与规则：{}".format(profile["customs"][:800]))
            if profile.get("proactive_topics"):
                lines.append("- 可主动关注：{}".format("、".join(profile["proactive_topics"][:15])))
        if confirmed:
            lines.append("已确认长期记忆：")
            lines.extend("- 用户{} [{}]：{}".format(
                item.get("subject_id", "?"), item.get("category", "fact"),
                item.get("content", "")[:200]) for item in confirmed)
        if goals:
            lines.append("进行中目标：")
            lines.extend("- {} [{}] {}".format(item.get("id"), item.get("status"), item.get("title", "")[:200]) for item in goals)
        if reminders:
            lines.append("待触发提醒：")
            lines.extend("- {} @{} {}".format(item.get("id"), int(item.get("due_at", 0)), item.get("text", "")[:200]) for item in reminders)
        if active_plans:
            lines.append("执行中的计划：")
            for plan in active_plans:
                pending = [step.get("title", "") for step in plan.get("steps", []) if step.get("status") not in {"done", "cancelled", "skipped"}]
                lines.append("- {} [{}] {}；下一步：{}".format(
                    plan.get("id"), plan.get("status"), plan.get("title", "")[:180],
                    " / ".join(pending[:3])[:400]))
        if insights:
            lines.append("已沉淀洞察：")
            lines.extend("- [{} {:.2f}] {}".format(
                item.get("category", "reflection"), float(item.get("confidence", 0)),
                item.get("content", "")[:240]) for item in insights)
        if skills:
            lines.append("匹配的技能/SOP：")
            lines.extend("- {}：{}".format(
                item.get("name", ""), item.get("instructions", "")[:800]) for item in skills)
        recent = [item for item in events[-12:] if item.get("event_id") != agent_event.event_id]
        if recent:
            lines.append("最近事件：")
            lines.extend("- 用户{}：{}".format(item.get("user_id"), item.get("text", "")[:200]) for item in recent)
        return "\n".join(lines)[:12000]
