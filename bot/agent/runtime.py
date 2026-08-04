"""Agent Core orchestration and staged autonomous execution."""

import hashlib
import json
import os
import time

from .context import AgentContextBuilder
from .executor import AgentExecutor
from .goals import AgentGoalStore
from .identity import resolve_identity, resolve_scope
from .memory import AgentMemory
from .models import AgentEvent
from .planner import AgentPlanner
from .policy import decide_event
from .proactive import ProactiveBudget
from .reminders import AgentReminderStore
from .response import can_autosend
from .storage.json_store import AgentJsonStore
from .tools.gateway import AgentToolGateway
from .workers import AgentTaskStore
from .verifier import AgentVerifier


class AgentRuntime:
    def __init__(self, config, root):
        self.config = config
        self.root = os.path.join(root, "data", "agent")
        self.store = AgentJsonStore(self.root)
        self.memory = AgentMemory(self.store)
        self.goals = AgentGoalStore(self.store)
        self.reminders = AgentReminderStore(self.store)
        self.proactive = ProactiveBudget(self.store)
        self.tasks = AgentTaskStore(self.store)
        self.context = AgentContextBuilder(self)
        self.planner = None
        self.tools = None
        self.executor = None
        self.verifier = None

    def build_event(self, event):
        scope, identity = resolve_scope(self.config, event), resolve_identity(self.config, event)
        seed = f"{scope.key}:{identity.user_id}:{event.get('message_id')}:{event.get('time', 0)}"
        return AgentEvent(
            hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24], scope, identity,
            str(event.get("raw_message") or "").strip(), str(event.get("raw_message") or ""),
            str(event.get("message_type") or ""), event.get("message_id"),
            float(event.get("time") or time.time()), {"post_type": event.get("post_type", "")},
        )

    def _record_event(self, agent_event, decision):
        if not agent_event.text:
            return
        self.store.append_bounded(f"events/{agent_event.scope.key.replace(':', '_')}.json", {
            "event_id": agent_event.event_id, "user_id": agent_event.identity.user_id,
            "level": int(agent_event.identity.level), "text": agent_event.text[:1000],
            "timestamp": agent_event.timestamp, "decision": decision.reason,
        }, limit=200)
        candidate = self.extract_memory_candidate(agent_event)
        if candidate:
            self.memory.add_candidate(candidate)

    def observe(self, event, *, explicit=False):
        agent_event = self.build_event(event)
        decision = decide_event(self.config, agent_event, explicit=explicit)
        self._record_event(agent_event, decision)
        return agent_event, decision

    def extract_memory_candidate(self, agent_event):
        text = agent_event.text.strip()
        markers = ("我喜欢", "我不喜欢", "我叫", "我的生日", "记住", "以后都")
        if len(text) < 4 or not text.startswith(markers):
            return None
        sensitive = any(word in text for word in ("生日", "住址", "密码", "隐私"))
        from .models import MemoryCandidate
        return MemoryCandidate(agent_event.scope.key, agent_event.identity.user_id, text[:500], 0.8, sensitive, agent_event.event_id)

    def _group_agent_enabled(self, agent_event):
        if agent_event.scope.is_private:
            return True
        group = self.config.get("groups", {}).get(str(agent_event.scope.group_id), {})
        return group.get("agent", {}).get("enabled", True)

    def _ensure_execution(self, dispatcher):
        if self.planner is None:
            self.planner = AgentPlanner(dispatcher)
        if self.tools is None:
            self.tools = AgentToolGateway(dispatcher)
        if self.executor is None:
            self.executor = AgentExecutor(self.tools, self.config)
        if self.verifier is None:
            self.verifier = AgentVerifier(dispatcher)
    async def run_autonomous(self, dispatcher, agent_event, *, task_context="", allow_background_queue=True):
        """Bounded plan/tool/replan loop, strongest only in super-owner private scope."""
        self._ensure_execution(dispatcher)
        settings = self.config.get("agent", {})
        owner_private = agent_event.identity.is_super_owner and agent_event.scope.is_private
        max_rounds = int(settings.get("owner_max_rounds", 6 if owner_private else 2))
        tool_budget = int(settings.get("owner_tool_budget", 12 if owner_private else 3))
        max_rounds = max(1, min(max_rounds, 10))
        tool_budget = max(0, min(tool_budget, 24))
        context = self.context.build(agent_event)
        catalog_method = getattr(self.tools, "catalog", None)
        tool_catalog = catalog_method() if callable(catalog_method) else {}
        if tool_catalog:
            context += "\n可用工具目录：\n" + "\n".join(
                "- {}: {}".format(name, description)
                for name, description in sorted(tool_catalog.items()))[:5000]
        if task_context:
            context += "\n后台任务上下文：" + task_context[:3000]
        transcript = []
        last_plan = None
        for round_index in range(max_rounds):
            round_context = context
            if transcript:
                round_context += "\n已执行结果：\n" + json.dumps(transcript[-6:], ensure_ascii=False)[:5000]
            plan = await self.planner.plan(agent_event, round_context)
            last_plan = plan
            tool_calls = plan.get("tools", [])
            if tool_calls and tool_budget > 0:
                results = await self.executor.execute(agent_event, tool_calls, remaining_budget=tool_budget)
                transcript.extend(results)
                tool_budget -= len(results)
                if results and round_index + 1 < max_rounds:
                    continue
            task = plan.get("task")
            if allow_background_queue and owner_private and task and task.get("goal"):
                queued = self.tasks.create(
                    agent_event.scope.key, agent_event.identity.user_id,
                    task["goal"], success_criteria=task.get("success_criteria", ""))
                plan["reply"] = (plan.get("reply") or "") + "\n已转为后台任务：{}".format(queued["id"])
            return plan, transcript
        return last_plan or {"reply": "暂时没规划好，稍后再继续。", "needs_confirmation": False}, transcript

    async def execute_background_task(self, dispatcher, task):
        """Execute one owner-private queued task and verify its result."""
        owner_id = int(task.get("owner_id", 0))
        event = AgentEvent(
            event_id="task:" + str(task.get("id", "")),
            scope=resolve_scope(self.config, {"message_type": "private", "user_id": owner_id}),
            identity=resolve_identity(self.config, {"message_type": "private", "user_id": owner_id}),
            text=str(task.get("goal", "")),
            raw_message=str(task.get("goal", "")),
            message_type="private",
            timestamp=time.time(),
            metadata={"background_task": True},
        )
        if not event.identity.is_super_owner or not event.scope.is_private:
            return {"success": False, "reason": "background_scope_denied", "reply": ""}
        plan, results = await self.run_autonomous(
            dispatcher, event,
            task_context="成功标准：{}".format(task.get("success_criteria", "")),
            allow_background_queue=False,
        )
        self._ensure_execution(dispatcher)
        verification = await self.verifier.verify(task, plan, results)
        return {
            "success": bool(verification.get("success")),
            "reason": verification.get("reason", ""),
            "evidence": verification.get("evidence", ""),
            "reply": str(plan.get("reply", ""))[:4000],
            "tool_results": results,
        }
    async def handle_event(self, dispatcher, event, *, explicit=False):
        agent_event, decision = self.observe(event, explicit=explicit)
        settings = self.config.get("agent", {})
        if not settings.get("primary_router", False) or settings.get("observation_only", True):
            return False
        if agent_event.scope.is_private and agent_event.identity.is_super_owner:
            if not settings.get("owner_autonomy_enabled", False):
                return False
        elif not agent_event.scope.is_private:
            group = self.config.get("groups", {}).get(str(agent_event.scope.group_id), {})
            if not group.get("agent", {}).get("primary_router", False):
                return False
        if not decision.should_reply or not self._group_agent_enabled(agent_event):
            return False
        if not agent_event.identity.is_super_owner and not agent_event.identity.can_manage_agent:
            return False
        plan, results = await self.run_autonomous(dispatcher, agent_event)
        allowed, reason = can_autosend(self.config, agent_event, plan)
        if not allowed:
            self.store.append_bounded("plans/rejected.json", {"event_id": agent_event.event_id, "reason": reason, "plan": plan}, limit=200)
            return False
        reply = str(plan.get("reply") or "").strip()
        if not reply:
            return False
        if agent_event.scope.is_private:
            await dispatcher.client.send_private_msg(agent_event.scope.owner_id, reply)
        else:
            await dispatcher.client.send_group_msg_with_at(agent_event.scope.group_id, reply, [agent_event.identity.user_id])
        self.proactive.record(self.config, agent_event.scope.key, topic=plan.get("intent", ""))
        self.store.append_bounded("plans/completed.json", {"event_id": agent_event.event_id, "plan": plan, "tool_results": results}, limit=200)
        return True
