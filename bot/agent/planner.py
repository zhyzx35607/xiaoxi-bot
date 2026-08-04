"""Structured planner using the existing DeepSeek provider, with safe defaults."""

import json

from ..ai import _call_deepseek


class AgentPlanner:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    async def plan(self, agent_event, context=""):
        prompt = (
            "你是 QQ Agent 的规划器，只输出一个 JSON 对象，不执行动作。"
            "字段：intent、reply、tools、needs_confirmation、reason、task、execution_plan、reflection。"
            "tools 是数组，每项格式 {{name, arguments, step_id}}；有 execution_plan 时 step_id 应对应其中步骤。"
            "工具必须已注册；不确定就空数组。"
            "task 可为空，或为 {{goal, success_criteria}}，用于需要后台持续处理的工作。"
            "execution_plan 可为空，或为 {{title, success_criteria, steps:[{{title, success_criteria}}]}}。"
            "只要请求包含多个可验证步骤、需要持续追踪或需要后台执行，就必须给 execution_plan。"
            "reflection 可为空，或为 {{content, category, confidence, evidence}}，只记录有证据、未来可复用的洞察。"
            "群主权限只作用于当前群；最高主人可管理全局。不要泄露私域记忆到群域。\n"
            "作用域={}; 身份等级={}; 消息={}\n{}".format(
                agent_event.scope.key, int(agent_event.identity.level),
                agent_event.text[:2000], context[:12000])
        )
        result = await _call_deepseek(
            self.dispatcher.config,
            [{"role": "system", "content": prompt}],
            max_tokens=max(700, min(int(self.dispatcher.config.get("agent", {}).get("planner_max_tokens", 1400)), 3000)),
            temperature=0.1,
            session=self.dispatcher.client.session,
        )
        fallback = {"intent": "none", "reply": "", "tools": [], "needs_confirmation": True, "reason": "planner_unavailable", "task": None, "execution_plan": None, "reflection": None}
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
                tools.append({
                    "name": str(item["name"])[:80], "arguments": arguments,
                    "step_id": str(item.get("step_id") or "")[:30],
                })
        task = data.get("task") if isinstance(data.get("task"), dict) else None
        if task:
            task = {"goal": str(task.get("goal", ""))[:1000], "success_criteria": str(task.get("success_criteria", ""))[:1000]}
        execution_plan = data.get("execution_plan") if isinstance(data.get("execution_plan"), dict) else None
        if execution_plan:
            steps = []
            for step in execution_plan.get("steps", [])[:20]:
                if isinstance(step, str):
                    steps.append({"title": step[:500], "success_criteria": ""})
                elif isinstance(step, dict) and (step.get("title") or step.get("step")):
                    steps.append({
                        "title": str(step.get("title") or step.get("step"))[:500],
                        "success_criteria": str(step.get("success_criteria") or "")[:500],
                    })
            execution_plan = {
                "title": str(execution_plan.get("title") or data.get("intent") or "执行计划")[:1000],
                "success_criteria": str(execution_plan.get("success_criteria") or "")[:1000],
                "steps": steps,
            } if steps else None
        reflection = data.get("reflection") if isinstance(data.get("reflection"), dict) else None
        if reflection:
            try:
                confidence = float(reflection.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            reflection = {
                "content": str(reflection.get("content") or "")[:1000],
                "category": str(reflection.get("category") or "reflection")[:50],
                "confidence": max(0.0, min(confidence, 1.0)),
                "evidence": str(reflection.get("evidence") or "")[:2000],
            } if reflection.get("content") else None
        return {
            "intent": str(data.get("intent", "none"))[:80],
            "reply": str(data.get("reply", ""))[:1500],
            "tools": tools[:8],
            "needs_confirmation": bool(data.get("needs_confirmation", True)),
            "reason": str(data.get("reason", ""))[:300],
            "task": task,
            "execution_plan": execution_plan,
            "reflection": reflection,
        }
