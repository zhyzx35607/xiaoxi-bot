"""Structured planner using the existing DeepSeek provider, with safe defaults."""

import json

from ..ai import _call_deepseek


class AgentPlanner:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    async def plan(self, agent_event, context=""):
        prompt = (
            "你是 QQ Agent 的规划器，只输出一个 JSON 对象，不执行动作。"
            "字段：intent、reply、tools、needs_confirmation、reason、task。"
            "tools 是数组，每项格式 {name, arguments}。工具必须只读且已注册；不确定就空数组。"
            "task 可为空，或为 {goal, success_criteria}，用于需要后台持续处理的工作。"
            "群主权限只作用于当前群；最高主人可管理全局。不要泄露私域记忆到群域。\n"
            "作用域={}; 身份等级={}; 消息={}\n{}".format(
                agent_event.scope.key, int(agent_event.identity.level),
                agent_event.text[:1200], context[:7000])
        )
        result = await _call_deepseek(
            self.dispatcher.config,
            [{"role": "system", "content": prompt}],
            max_tokens=700,
            temperature=0.1,
            session=self.dispatcher.client.session,
        )
        fallback = {"intent": "none", "reply": "", "tools": [], "needs_confirmation": True, "reason": "planner_unavailable", "task": None}
        if not result:
            return fallback
        try:
            data = json.loads(result.strip().removeprefix("```json").removesuffix("```").strip())
        except (TypeError, json.JSONDecodeError):
            return {**fallback, "intent": "chat", "reply": str(result)[:1500], "reason": "unstructured_planner_output"}
        if not isinstance(data, dict):
            return fallback
        tools = []
        for item in data.get("tools", []):
            if isinstance(item, str):
                tools.append({"name": item[:80], "arguments": {}})
            elif isinstance(item, dict) and item.get("name"):
                arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
                tools.append({"name": str(item["name"])[:80], "arguments": arguments})
        task = data.get("task") if isinstance(data.get("task"), dict) else None
        if task:
            task = {"goal": str(task.get("goal", ""))[:1000], "success_criteria": str(task.get("success_criteria", ""))[:1000]}
        return {
            "intent": str(data.get("intent", "none"))[:80],
            "reply": str(data.get("reply", ""))[:1500],
            "tools": tools[:8],
            "needs_confirmation": bool(data.get("needs_confirmation", True)),
            "reason": str(data.get("reason", ""))[:300],
            "task": task,
        }
