"""Agent Core orchestration and staged autonomous execution."""

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import replace

from ..memory import contains_sensitive_data
from ..permission import get_bot_role
from ..ai.prompts import _should_lookup_bot_help
from ..ai.reply import strip_command_prefix
from .context import AgentContextBuilder
from .companion_runtime import CompanionRuntime
from .executor import AgentExecutor
from .goals import AgentGoalStore
from .identity import resolve_identity, resolve_scope
from .memory import AgentMemory
from .models import AgentEvent
from .insights import AgentInsightStore
from .plans import AgentPlanStore
from .planner import AgentPlanner
from .policy import decide_event, primary_router_enabled
from .profiles import AgentProfileStore
from .proactive import ProactiveBudget
from .reminders import AgentReminderStore
from .response import can_autosend
from .skills import AgentSkillStore
from .storage.json_store import AgentJsonStore
from .timeline import AgentTimeline
from .tools.gateway import AgentToolGateway
from .tools.napcat import MODERATION_ACTIONS
from .tools.native import WRITE_TOOLS
from .workers import AgentTaskStore
from .verifier import AgentVerifier


def _tool_requires_confirmation(name):
    """Single-tool view of the confirmation rule (native writes + moderation)."""
    return name in WRITE_TOOLS or name in MODERATION_ACTIONS


def _plan_requires_confirmation(plan):
    """Deterministic confirmation rule for Agent plans.

    AI output is not proof of safety: a plan containing native write tools,
    moderation tools or an execution_plan always needs human confirmation,
    regardless of the model's self-assessed needs_confirmation flag.
    """
    if not isinstance(plan, dict):
        return True
    if plan.get("execution_plan"):
        return True
    for tool in plan.get("tools") or []:
        name = tool.get("name") if isinstance(tool, dict) else tool
        if _tool_requires_confirmation(name):
            return True
    return False


class AgentRuntime:
    def __init__(self, config, root):
        self.config = config
        self.root = os.path.join(root, "data", "agent")
        self.store = AgentJsonStore(self.root)
        self.memory = AgentMemory(self.store)
        self.insights = AgentInsightStore(self.store)
        self.goals = AgentGoalStore(self.store)
        self.plans = AgentPlanStore(self.store)
        self.profiles = AgentProfileStore(self.store)
        self.reminders = AgentReminderStore(self.store)
        self.skills = AgentSkillStore(self.store)
        self.timeline = AgentTimeline(self.store)
        self.proactive = ProactiveBudget(self.store)
        self.tasks = AgentTaskStore(self.store)
        self.companion = CompanionRuntime(config, self.root)
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
        event_limit = max(12, min(
            int(self.config.get("agent", {}).get("event_history_limit", 100)), 200))
        event_text = "[敏感内容已省略]" if contains_sensitive_data(agent_event.text) else agent_event.text[:1000]
        self.store.append_bounded_unique(f"events/{agent_event.scope.key.replace(':', '_')}.json", {
            "event_id": agent_event.event_id, "user_id": agent_event.identity.user_id,
            "level": int(agent_event.identity.level), "text": event_text,
            "timestamp": agent_event.timestamp, "decision": decision.reason,
        }, key="event_id", limit=event_limit)
        candidate = self.extract_memory_candidate(agent_event)
        if candidate:
            self.memory.add_candidate(candidate)

    def observe(self, event, *, explicit=False):
        agent_event = self.build_event(event)
        decision = decide_event(self.config, agent_event, explicit=explicit)
        self._record_event(agent_event, decision)
        self._apply_proactive_feedback(agent_event)
        return agent_event, decision

    def _apply_proactive_feedback(self, agent_event):
        if not agent_event.identity.can_manage_agent:
            return
        text = re.sub(r"[，。！？!?,.\s]", "", agent_event.text.lower())
        reject_markers = ("别主动", "不要主动", "别再发", "不用提醒", "安静点", "别打扰", "停止主动")
        resume_markers = ("恢复主动", "可以主动", "继续主动")
        if any(marker in text for marker in reject_markers):
            seconds = max(3600, int(self.config.get("agent", {}).get("rejection_mute_seconds", 43200)))
            self.proactive.mute(agent_event.scope.key, seconds=seconds, now=agent_event.timestamp)
            self.timeline.add(
                agent_event.scope.key, "proactive_muted", "收到明确拒绝，暂停主动消息",
                actor_id=agent_event.identity.user_id, metadata={"seconds": seconds})
        elif any(marker in text for marker in resume_markers):
            self.proactive.unmute(agent_event.scope.key)
            self.timeline.add(
                agent_event.scope.key, "proactive_resumed", "收到恢复主动指令",
                actor_id=agent_event.identity.user_id)

    def extract_memory_candidate(self, agent_event):
        text = agent_event.text.strip()
        if len(text) < 4 or len(text) > 500:
            return None
        if contains_sensitive_data(text):
            return None
        patterns = (
            ("preference", r"^(?:我|本人)(?:最)?(?:喜欢|偏好|常用|不喜欢|讨厌|希望).+"),
            ("identity", r"^(?:我叫|叫我|我的昵称是|我的称呼是).+"),
            ("commitment", r"^(?:记住|以后(?:都|请)|下次(?:要|请)|长期).+"),
            ("correction", r"^(?:不是.+而是|纠正一下|更正一下|你记错了).+"),
            ("group_rule", r"^(?:本群|这个群|群里)(?:以后|规则|习惯|默认|不要|应该).+"),
        )
        category = next((name for name, pattern in patterns if re.match(pattern, text, re.I)), "")
        if not category:
            return None
        sensitive = any(word in text for word in ("生日", "住址", "地址", "电话", "手机号", "身份证", "隐私"))
        group_personal = not agent_event.scope.is_private and category != "group_rule"
        requires_confirmation = sensitive or group_personal
        confidence = 0.95 if category in {"correction", "group_rule"} else 0.8
        from .models import MemoryCandidate
        return MemoryCandidate(
            agent_event.scope.key, agent_event.identity.user_id, text[:500], confidence,
            requires_confirmation, agent_event.event_id, category)

    def _group_agent_enabled(self, agent_event):
        if agent_event.scope.is_private:
            return True
        group = self.config.get("groups", {}).get(str(agent_event.scope.group_id), {})
        return group.get("agent", {}).get("enabled", True)

    def primary_router_enabled(self, event):
        return primary_router_enabled(self.config, self.build_event(event))

    def _ensure_execution(self, dispatcher):
        if self.planner is None:
            self.planner = AgentPlanner(dispatcher)
        if self.tools is None:
            self.tools = AgentToolGateway(dispatcher)
        if self.executor is None:
            self.executor = AgentExecutor(self.tools, self.config)
        if self.verifier is None:
            self.verifier = AgentVerifier(dispatcher)

    async def _planning_context(self, dispatcher, agent_event, task_context=""):
        context = self.context.build(agent_event)
        if not agent_event.scope.is_private:
            # AI 输出不是权限证明，但身份线索能让 planner 做出符合角色的规划；
            # 真正的处置权限仍在 napcat_moderation 里逐次复核。
            bot_role, _ = await get_bot_role(dispatcher, agent_event.scope.group_id)
            role_name = {"owner": "群主", "admin": "管理"}.get(bot_role, "成员")
            context += "\n小汐在本群的身份：" + role_name
            if bot_role in ("owner", "admin"):
                context += "；你是本群管理，发现广告/刷屏/违规时应主动维护秩序"
            else:
                context += "；你只是成员，只观察不处置"
        catalog_method = getattr(self.tools, "catalog", None)
        tool_catalog = catalog_method(agent_event) if callable(catalog_method) else {}
        if tool_catalog:
            context += "\n可用工具目录：\n" + "\n".join(
                "- {}: {}".format(name, description)
                for name, description in sorted(tool_catalog.items()))[:5000]
        # 功能咨询类问题：确定性注入帮助摘要，与普通 AI 聊天同机制
        if _should_lookup_bot_help(agent_event.raw_message):
            from ..commands.system import build_help_digest
            help_status, _name, help_text = build_help_digest(
                getattr(dispatcher, "commands", {}) or {},
                int(agent_event.identity.level), "",
                group_id=0 if agent_event.scope.is_private
                else int(agent_event.scope.group_id or 0))
            if help_status == "ok" and help_text:
                context += (
                    "\n【小汐功能参考】对方正在问你（小汐）自身的功能/命令用法。"
                    "以下是按对方身份过滤后的真实功能清单：必须照清单直接回答，"
                    "禁止说不知道/没弄过/不清楚；清单里确实没有的才说没有。\n"
                    + help_text)
        if task_context:
            context += "\n后台任务上下文：" + task_context[:3000]
        return context

    async def run_autonomous(self, dispatcher, agent_event, *, task_context="", allow_background_queue=True, initial_plan=None, allow_replanned_tools=True, read_only_tools=False):
        """Bounded plan/tool/replan loop, strongest only in super-owner private scope."""
        self._ensure_execution(dispatcher)
        settings = self.config.get("agent", {})
        owner_private = agent_event.identity.is_super_owner and agent_event.scope.is_private
        max_rounds = int(settings.get(
            "owner_max_rounds" if owner_private else "group_max_rounds",
            6 if owner_private else 3))
        tool_budget = int(settings.get(
            "owner_tool_budget" if owner_private else "group_tool_budget",
            12 if owner_private else 5))
        max_rounds = max(1, min(max_rounds, 10))
        tool_budget = max(0, min(tool_budget, 24))
        context = await self._planning_context(dispatcher, agent_event, task_context)
        transcript = []
        last_plan = None
        persisted_plan = None
        for round_index in range(max_rounds):
            round_context = context
            if transcript:
                round_context += "\n已执行结果：\n" + json.dumps(transcript[-6:], ensure_ascii=False)[:5000]
            plan = initial_plan if round_index == 0 and initial_plan is not None else await self.planner.plan(agent_event, round_context)
            last_plan = plan
            if persisted_plan is None and plan.get("execution_plan"):
                specification = plan["execution_plan"]
                persisted_plan = self.plans.create(
                    agent_event.scope.key, agent_event.identity.user_id,
                    specification.get("title", agent_event.text),
                    specification.get("steps", []),
                    success_criteria=specification.get("success_criteria", ""),
                    source_event_id=agent_event.event_id,
                )
                plan["plan_id"] = persisted_plan["id"]
                self.timeline.add(
                    agent_event.scope.key, "plan_created", persisted_plan["title"],
                    actor_id=agent_event.identity.user_id,
                    metadata={"plan_id": persisted_plan["id"], "steps": len(persisted_plan["steps"])},
                )
            elif persisted_plan:
                plan["plan_id"] = persisted_plan["id"]
            tool_calls = plan.get("tools", [])
            if round_index > 0 and not allow_replanned_tools:
                tool_calls = []
            if read_only_tools:
                is_read_only = getattr(self.tools, "is_read_only", lambda name: False)
                tool_calls = [item for item in tool_calls if is_read_only(str(item.get("name") or ""))]
            if (tool_calls and round_index > 0
                    and not agent_event.identity.is_super_owner
                    and not agent_event.metadata.get("confirmed")):
                # 重规划轮次未经 handle_event 的确认门控：非最高主人触发且未走
                # 确认流时，剔除需要人工确认的高风险工具，防止借重规划绕过
                # _plan_requires_confirmation（例如补一个 delete_msg 无确认执行）。
                tool_calls = [
                    item for item in tool_calls
                    if not _tool_requires_confirmation(
                        item.get("name") if isinstance(item, dict) else item)
                ]
            if tool_calls and tool_budget > 0:
                results = await self.executor.execute(agent_event, tool_calls, remaining_budget=tool_budget)
                transcript.extend(results)
                for result in results:
                    payload = result.get("result") if isinstance(result, dict) else {}
                    ok = bool(isinstance(payload, dict) and payload.get("ok", payload.get("status") in {None, "ok"}))
                    step_id = result.get("step_id", "")
                    if persisted_plan and step_id:
                        self.plans.update_step(
                            agent_event.scope.key, persisted_plan["id"], step_id,
                            "done" if ok else "failed",
                            evidence=json.dumps(result, ensure_ascii=False)[:2000],
                            result=str(payload)[:2000],
                        )
                    self.timeline.add(
                        agent_event.scope.key, "tool_result",
                        "{} {}".format(result.get("name", "tool"), "成功" if ok else "失败"),
                        actor_id=agent_event.identity.user_id,
                        evidence=json.dumps(result, ensure_ascii=False)[:2000],
                        metadata={"event_id": agent_event.event_id, "plan_id": plan.get("plan_id", "")},
                    )
                tool_budget -= len(results)
                if results and round_index + 1 < max_rounds:
                    continue
            task = plan.get("task")
            if allow_background_queue and owner_private and task and task.get("goal"):
                queued = self.tasks.create(
                    agent_event.scope.key, agent_event.identity.user_id,
                    task["goal"], success_criteria=task.get("success_criteria", ""),
                    plan_id=persisted_plan["id"] if persisted_plan else "")
                plan["reply"] = (plan.get("reply") or "") + "\n已转为后台任务：{}".format(queued["id"])
            reflection = plan.get("reflection")
            if reflection and (reflection.get("evidence") or transcript):
                self.insights.add(
                    agent_event.scope.key, reflection.get("content", ""),
                    category=reflection.get("category", "reflection"),
                    confidence=reflection.get("confidence", 0.5),
                    evidence=reflection.get("evidence") or json.dumps(transcript[-3:], ensure_ascii=False),
                    source_id=agent_event.event_id,
                )
            self.timeline.add(
                agent_event.scope.key, "agent_reply", plan.get("reply", "")[:1000],
                actor_id=agent_event.identity.user_id,
                metadata={"event_id": agent_event.event_id, "plan_id": plan.get("plan_id", "")},
            )
            return plan, transcript
        return last_plan or {"reply": "暂时没规划好，稍后再继续。", "needs_confirmation": False}, transcript

    async def execute_confirmed_plan(self, dispatcher, event, plan, *, role="owner"):
        event = dict(event or {})
        event.setdefault("sender", {})
        event["sender"]["role"] = role or event["sender"].get("role") or "owner"
        agent_event = self.build_event(event)
        # 人工确认过的计划获得 confirmed 标记，高风险群管工具只认这个标记
        # （或最高主人身份），模型自己编不出。
        agent_event = replace(
            agent_event, metadata={**dict(agent_event.metadata), "confirmed": True})
        if agent_event.scope.is_private or not agent_event.identity.can_manage_agent:
            return {"success": False, "reason": "confirmed_scope_denied"}
        frozen_plan = dict(plan or {})
        frozen_plan["needs_confirmation"] = False
        final_plan, results = await self.run_autonomous(
            dispatcher, agent_event, allow_background_queue=False,
            initial_plan=frozen_plan, allow_replanned_tools=False)
        reply = strip_command_prefix(str(final_plan.get("reply") or "").strip())
        self.timeline.add(
            agent_event.scope.key, "confirmed_plan_executed", reply or frozen_plan.get("intent", "Agent 方案"),
            actor_id=agent_event.identity.user_id,
            evidence=json.dumps(results, ensure_ascii=False)[:2000],
            metadata={"event_id": agent_event.event_id})
        return {"success": True, "message": reply or "Agent 方案已执行", "tool_results": results}

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
        if not primary_router_enabled(self.config, agent_event):
            return False
        if not decision.should_reply or not self._group_agent_enabled(agent_event):
            return False
        # 显式呼叫（@bot/叫名字/回复 bot）不卡身份，普通成员也进规划流程；
        # 写工具在 native/moderation 工具层按身份逐次门控，规划层不加锁。
        # 非显式消息维持 member_passive_only 的现状（decide_event 已拦截）。
        if (not explicit
                and not agent_event.identity.is_super_owner
                and not agent_event.identity.can_manage_agent):
            return False
        initial_plan = None
        if not agent_event.identity.is_super_owner:
            self._ensure_execution(dispatcher)
            initial_plan = await self.planner.plan(
                agent_event, await self._planning_context(dispatcher, agent_event))
            # The model may only tighten confirmation, never waive it.
            if _plan_requires_confirmation(initial_plan) or bool(initial_plan.get("needs_confirmation", True)):
                from ..services.confirmations import create_agent_confirmation
                frozen_event = {
                    "user_id": agent_event.identity.user_id,
                    "group_id": agent_event.scope.group_id,
                    "message_type": "group",
                    "raw_message": agent_event.text,
                    "message_id": agent_event.message_id,
                    "time": agent_event.timestamp,
                    "sender": {"role": agent_event.identity.role},
                }
                code = create_agent_confirmation(
                    agent_event.scope.group_id, agent_event.identity.user_id,
                    frozen_event, initial_plan,
                    initial_plan.get("reason") or initial_plan.get("intent") or "Agent 方案")
                await dispatcher.client.send_group_msg_with_at(
                    agent_event.scope.group_id,
                    "这个 Agent 方案需要你确认。发送 /确认 {}，一分钟内有效。".format(code),
                    [agent_event.identity.user_id])
                self.timeline.add(
                    agent_event.scope.key, "confirmation_requested",
                    initial_plan.get("reason") or initial_plan.get("intent") or "Agent 方案",
                    actor_id=agent_event.identity.user_id,
                    metadata={"event_id": agent_event.event_id, "confirmation_code": code})
                return True
        plan, results = await self.run_autonomous(
            dispatcher, agent_event, initial_plan=initial_plan)
        allowed, reason = can_autosend(self.config, agent_event, plan, explicit=explicit)
        if not allowed:
            self.store.append_bounded("plans/rejected.json", {"event_id": agent_event.event_id, "reason": reason, "plan": plan}, limit=200)
            return False
        # AI 文本行首的 "/" 会被 message_sent 回环当主人命令执行，外发前一律中和
        reply = strip_command_prefix(str(plan.get("reply") or "").strip())
        if not reply:
            return False
        if agent_event.scope.is_private:
            repetitive = getattr(dispatcher, "_owner_reply_is_repetitive", None)
            if callable(repetitive) and repetitive(reply):
                self.timeline.add(
                    agent_event.scope.key, "reply_suppressed",
                    "相似回复已抑制", actor_id=agent_event.identity.user_id,
                    metadata={"event_id": agent_event.event_id})
                return True
            await dispatcher.client.send_private_msg(agent_event.scope.owner_id, reply)
            record_reply = getattr(dispatcher, "_record_owner_reply", None)
            if callable(record_reply):
                record_reply(reply)
            companion = getattr(self, "companion", None)
            if companion is not None:
                await asyncio.to_thread(
                    companion.observe_outgoing, reply,
                    topic=plan.get("intent", "conversation"))
        else:
            await dispatcher.client.send_group_msg_with_at(agent_event.scope.group_id, reply, [agent_event.identity.user_id])
        if decision.reason != "explicit_request":
            self.proactive.record(
                self.config, agent_event.scope.key, topic=plan.get("intent", ""))
        self.store.append_bounded("plans/completed.json", {"event_id": agent_event.event_id, "plan": plan, "tool_results": results}, limit=200)
        return True
