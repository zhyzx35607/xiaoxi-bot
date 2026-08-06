"""Verify autonomous task results against explicit success criteria."""

import json

from ..ai import _call_deepseek


class AgentVerifier:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    async def verify(self, task, plan, tool_results):
        criteria = str(task.get("success_criteria") or "").strip()
        if not criteria:
            return {"success": bool(plan.get("reply")), "reason": "no_explicit_criteria"}
        prompt = (
            "你是 Agent 任务验收器，只输出 JSON：{{success:boolean, reason:string, evidence:string}}。"
            "必须依据工具结果和最终答复判断，不允许因为语气自信就通过。\n"
            "任务：{}\n成功标准：{}\n最终答复：{}\n工具结果：{}"
        ).format(task.get("goal", "")[:1000], criteria[:1000], str(plan.get("reply", ""))[:2000], json.dumps(tool_results, ensure_ascii=False)[:5000])
        result = await _call_deepseek(
            self.dispatcher.config,
            [{"role": "system", "content": prompt}],
            max_tokens=250,
            temperature=0.0,
            session=self.dispatcher.client.session,
        )
        try:
            data = json.loads(str(result).strip().removeprefix("```json").removesuffix("```").strip())
        except (TypeError, json.JSONDecodeError):
            return {"success": False, "reason": "verifier_unavailable", "evidence": ""}
        return {
            "success": bool(data.get("success", False)),
            "reason": str(data.get("reason", ""))[:300],
            "evidence": str(data.get("evidence", ""))[:1000],
        }
