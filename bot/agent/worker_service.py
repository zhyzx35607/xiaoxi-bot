"""Lifecycle worker for reminders, owner tasks, and goal reviews."""

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from urllib.parse import urlparse

from .policy import is_quiet_hours
from .storage.json_store import new_record_id
from ..utils import bot_timezone, configured_timezone_name

log = logging.getLogger("qqbot")


def _safe_media_ref(value):
    value = str(value or "").strip()[:2000]
    if value.startswith("file://"):
        return value
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
        return value
    return ""


class AgentWorker:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self._task = None
        self._stop = asyncio.Event()

    def start(self):
        settings = self.dispatcher.config.get("agent", {})
        if not settings.get("enabled", True) or not settings.get("worker_enabled", True):
            return None
        if self._task and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agent-worker")
        return self._task

    async def stop(self):
        self._stop.set()
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self):
        settings = self.dispatcher.config.get("agent", {})
        interval = max(10, int(settings.get("worker_interval_seconds", 30)))
        try:
            await self.tick()
        except Exception:
            log.exception("Agent worker tick failed")
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await self.tick()
                except Exception:
                    log.exception("Agent worker tick failed")

    async def _deliver_reminders(self):
        delivered = failed = 0
        for reminder in self.dispatcher.agent_runtime.reminders.due(limit=20):
            try:
                text = "\u63d0\u9192\u4f60\uff1a{}".format(reminder.get("text", ""))
                scope_key = str(reminder.get("scope_key", ""))
                if scope_key.startswith("group:"):
                    result = await self.dispatcher.client.send_group_msg_with_at(
                        int(scope_key.split(":", 1)[1]), text,
                        [int(reminder.get("user_id", 0))])
                else:
                    result = await self.dispatcher.client.send_private_msg(
                        int(reminder.get("user_id", 0)), text)
                if isinstance(result, dict) and result.get("status") not in {None, "ok"}:
                    raise RuntimeError(result.get("message") or result.get("msg") or result)
                self.dispatcher.agent_runtime.reminders.mark(reminder["id"], "sent")
                delivered += 1
            except Exception as error:
                attempts = int(reminder.get("attempts", 0)) + 1
                self.dispatcher.agent_runtime.reminders.mark(
                    reminder["id"], "failed" if attempts >= 3 else "pending", str(error))
                failed += 1
                log.warning("Agent reminder delivery failed id=%s attempt=%s: %s",
                            reminder.get("id"), attempts, error)
        return delivered, failed

    async def _deliver_companion_outbox(self):
        runtime = self.dispatcher.agent_runtime
        companion = getattr(runtime, "companion", None)
        owner_id = int(self.dispatcher.config.get("bot_owner") or 0)
        if companion is None or not owner_id:
            return 0, 0
        delivered = failed = 0
        max_attempts = max(1, int(self.dispatcher.config.get("agent", {}).get(
            "companion_outbox_max_attempts", 3)))
        for item in companion.store.due_outbox(owner_id, time.time(), limit=10):
            if time.time() - float(getattr(
                    self.dispatcher, "_owner_last_incoming_at", 0) or 0) < 15:
                continue
            try:
                payload = item.get("payload") or {}
                parts = payload.get("message_parts") or []
                if isinstance(parts, str):
                    parts = [parts]
                parts = parts[:1] if item.get("priority") != "urgent" else parts[:4]
                combined_text = "\n".join(
                    str(part).strip() for part in parts if str(part).strip())
                repetitive = getattr(
                    self.dispatcher, "_owner_reply_is_repetitive", None)
                if callable(repetitive) and repetitive(combined_text):
                    companion.store.mark_outbox(
                        item["id"], "suppressed", "similar to a recent owner reply")
                    log.info("Companion outbox suppressed repetitive item id=%s", item.get("id"))
                    continue
                for index, text in enumerate(parts[:4]):
                    if not str(text).strip():
                        continue
                    result = await self.dispatcher.client.send_private_msg(
                        owner_id, [{"type": "text", "data": {"text": str(text).strip()}}])
                    if isinstance(result, dict) and result.get("status") not in {None, "ok"}:
                        raise RuntimeError(result.get("message") or result.get("msg") or result)
                    if index < len(parts) - 1:
                        await asyncio.sleep(random.uniform(0.5, 1.8))
                media = payload.get("media_request") or {}
                media_kind = str(media.get("kind") or "").lower()
                media_file = _safe_media_ref(media.get("file") or media.get("url"))
                if (companion.state().get("media_enabled", True) and not media_file
                        and media_kind == "image" and media.get("query")):
                    try:
                        from ..ai import generate_image
                        media_file, media_error = await generate_image(
                            self.dispatcher, str(media.get("query"))[:500])
                        media_file = _safe_media_ref(media_file)
                        if media_error:
                            log.debug("Companion image generation skipped: %s", media_error)
                    except Exception as error:
                        log.debug("Companion image generation failed: %s", error)
                    if not media_file:
                        try:
                            from ..integrations.uapi import uapi_resolve_image_url
                            media_file = _safe_media_ref(await uapi_resolve_image_url(
                                self.dispatcher, "/image/bing-daily"))
                        except Exception as error:
                            log.debug("Companion UApi image fallback failed: %s", error)
                if companion.state().get("media_enabled", True) and media_file:
                    segment_type = "video" if media_kind == "video" else "image"
                    result = await self.dispatcher.client.send_private_msg(
                        owner_id, [{"type": segment_type, "data": {"file": media_file}}])
                    if isinstance(result, dict) and result.get("status") not in {None, "ok"}:
                        if media_kind == "video":
                            await self.dispatcher.client.send_private_msg(owner_id, media_file)
                        else:
                            raise RuntimeError(result.get("message") or result.get("msg") or result)
                companion.store.mark_outbox(item["id"], "sent")
                record_reply = getattr(self.dispatcher, "_record_owner_reply", None)
                if callable(record_reply):
                    record_reply(combined_text)
                companion.observe_outgoing("\n".join(str(part) for part in parts), topic=item.get("topic", ""))
                runtime.proactive.record(self.dispatcher.config, "owner:{}".format(owner_id), topic=item.get("topic", ""))
                delivered += 1
            except Exception as error:
                attempts = int(item.get("attempts", 0)) + 1
                status = "failed" if attempts >= max_attempts else "pending"
                retry_at = time.time() + min(3600, 30 * (2 ** max(0, attempts - 1)))
                companion.store.mark_outbox(item["id"], status, str(error), retry_at if status == "pending" else None)
                failed += 1
                log.warning("Companion outbox delivery failed id=%s attempt=%s: %s",
                            item.get("id"), attempts, error)
        return delivered, failed

    async def _run_owner_companion(self):
        try:
            settings = self.dispatcher.config.get("agent", {})
            companion = getattr(self.dispatcher.agent_runtime, "companion", None)
            owner_id = int(self.dispatcher.config.get("bot_owner") or 0)
            if not companion or not owner_id or not settings.get("companion_enabled", True):
                return "disabled"
            now = time.time()
            reason, payload = companion._due_reason(now)
            high_priority = reason == "event"
            local_now = datetime.fromtimestamp(
                now, bot_timezone(configured_timezone_name(self.dispatcher.config)))
            if is_quiet_hours(settings, local_now) and not high_priority:
                return "quiet_hours"
            allowed, budget_reason = self.dispatcher.agent_runtime.proactive.allowed(
                self.dispatcher.config, "owner:{}".format(owner_id),
                topic=(payload or {}).get("topic", reason) if isinstance(payload, dict) else reason,
                is_private=True, now=now, priority="urgent" if high_priority else "normal")
            if not allowed and not (high_priority and budget_reason == "quiet_hours"):
                return budget_reason
            result = await companion.decide(self.dispatcher, now=now)
            return "queued" if result else "idle"
        except Exception as error:
            log.warning("Owner companion run failed: %s", error)
            return "error"

    async def _run_owner_task(self):
        settings = self.dispatcher.config.get("agent", {})
        if not settings.get("owner_autonomy_enabled", False) or not settings.get("background_tasks_enabled", True):
            return "disabled"
        lease_seconds = settings.get("background_task_lease_seconds", 3600)
        task = self.dispatcher.agent_runtime.tasks.next_queued(lease_seconds)
        if not task:
            return "idle"
        self.dispatcher.agent_runtime.tasks.update(task["id"], "running")
        runtime = self.dispatcher.agent_runtime
        runtime.timeline.add(
            task.get("scope_key", ""), "task_started", task.get("goal", ""),
            actor_id=task.get("owner_id", 0), metadata={"task_id": task.get("id", ""), "plan_id": task.get("plan_id", "")})
        try:
            result = await self.dispatcher.agent_runtime.execute_background_task(
                self.dispatcher, task)
            attempts = int(task.get("attempts", 0)) + 1
            if result.get("success"):
                runtime.tasks.update(
                    task["id"], "done", json.dumps(result, ensure_ascii=False))
                try:
                    self._update_linked_plan(task, "done", result)
                    runtime.timeline.add(
                        task.get("scope_key", ""), "task_done", task.get("goal", ""),
                        actor_id=task.get("owner_id", 0), evidence=result.get("evidence", ""),
                        metadata={"task_id": task.get("id", ""), "plan_id": task.get("plan_id", "")})
                except Exception:
                    log.exception("Agent task completion bookkeeping failed id=%s", task.get("id"))
                summary = "\u540e\u53f0\u4efb\u52a1 {} \u5df2\u5b8c\u6210\n{}".format(
                    task["id"], result.get("reply") or result.get("evidence") or "\u5df2\u901a\u8fc7\u9a8c\u6536")
                try:
                    await self.dispatcher.client.send_private_msg(
                        int(task["owner_id"]), summary[:3500])
                except Exception as error:
                    log.warning("Agent task completion notification failed id=%s: %s", task.get("id"), error)
                return "done"
            max_attempts = int(settings.get("background_task_max_attempts", 3))
            status = "failed" if attempts >= max_attempts else "queued"
            runtime.tasks.update(
                task["id"], status, json.dumps(result, ensure_ascii=False),
                result.get("reason", ""))
            if status == "failed":
                self._update_linked_plan(task, "failed", result)
                runtime.timeline.add(
                    task.get("scope_key", ""), "task_failed", task.get("goal", ""),
                    actor_id=task.get("owner_id", 0), evidence=result.get("reason", ""),
                    metadata={"task_id": task.get("id", ""), "plan_id": task.get("plan_id", "")})
                try:
                    await self.dispatcher.client.send_private_msg(
                        int(task["owner_id"]),
                        "\u540e\u53f0\u4efb\u52a1 {} \u8fde\u7eed\u5931\u8d25\uff0c\u5df2\u505c\u6b62\uff1a{}".format(
                            task["id"], result.get("reason", "\u672a\u77e5\u539f\u56e0"))[:2000])
                except Exception as error:
                    log.warning("Agent task failure notification failed id=%s: %s", task.get("id"), error)
            return status
        except Exception as error:
            attempts = int(task.get("attempts", 0)) + 1
            max_attempts = int(settings.get("background_task_max_attempts", 3))
            status = "failed" if attempts >= max_attempts else "queued"
            runtime.tasks.update(
                task["id"], status, error=str(error))
            if status == "failed":
                self._update_linked_plan(task, "failed", {"reason": str(error)})
                runtime.timeline.add(
                    task.get("scope_key", ""), "task_failed", task.get("goal", ""),
                    actor_id=task.get("owner_id", 0), evidence=str(error),
                    metadata={"task_id": task.get("id", ""), "plan_id": task.get("plan_id", "")})
            log.exception("Agent background task failed id=%s", task.get("id"))
            return status

    def _update_linked_plan(self, task, status, result):
        plan_id = str(task.get("plan_id") or "")
        scope_key = str(task.get("scope_key") or "")
        if not plan_id or not scope_key:
            return None
        plan = self.dispatcher.agent_runtime.plans.get(scope_key, plan_id)
        if not plan:
            return None
        step = next((item for item in plan.get("steps", []) if item.get("status") not in {"done", "failed", "cancelled", "skipped"}), None)
        if not step:
            return plan
        return self.dispatcher.agent_runtime.plans.update_step(
            scope_key, plan_id, step.get("id"), status,
            evidence=result.get("evidence") or result.get("reason") or "",
            result=result.get("reply") or "",
        )

    @staticmethod
    def _review_lease_seconds(settings):
        try:
            return max(300, int(settings.get("review_lease_seconds", 3600)))
        except (TypeError, ValueError):
            return 3600

    @classmethod
    def _review_in_progress(cls, state, now, lease_seconds):
        if not isinstance(state, dict) or state.get("status") != "running":
            return False
        started_at = float(state.get("started_at", 0) or 0)
        return bool(started_at and now - started_at < lease_seconds)

    async def _review_owner_goal(self):
        settings = self.dispatcher.config.get("agent", {})
        owner_id = int(self.dispatcher.config.get("bot_owner") or 0)
        if not owner_id or not settings.get("owner_autonomy_enabled", False):
            return "disabled"
        scope_key = "owner:{}".format(owner_id)
        goals = self.dispatcher.agent_runtime.goals.list(scope_key)
        if not goals:
            return "no_goals"
        interval = max(1800, int(settings.get("owner_goal_check_interval_seconds", 7200)))
        state = self.dispatcher.agent_runtime.store.read(
            "worker/owner_goal_review.json", {})
        lease_seconds = self._review_lease_seconds(settings)
        last_run = float(state.get("last_run", 0) or 0) if isinstance(state, dict) else 0
        now = time.time()
        if self._review_in_progress(state, now, lease_seconds):
            return "in_progress"
        if now - last_run < interval:
            return "cooldown"
        allowed, reason = self.dispatcher.agent_runtime.proactive.allowed(
            self.dispatcher.config, scope_key, topic="goal-review",
            is_private=True, now=now)
        if not allowed:
            return reason
        goal = goals[0]
        run_id = new_record_id(16)
        self.dispatcher.agent_runtime.store.write(
            "worker/owner_goal_review.json",
            {"status": "running", "run_id": run_id, "started_at": now, "goal_id": goal.get("id")},
        )
        event = self.dispatcher.agent_runtime.build_event({
            "user_id": owner_id,
            "message_type": "private",
            "raw_message": "\u8bf7\u4e3b\u52a8\u590d\u76d8\u5e76\u63a8\u8fdb\u8fd9\u4e2a\u957f\u671f\u76ee\u6807\uff1a{}".format(
                goal.get("title", "")),
            "time": now,
        })
        try:
            plan, results = await self.dispatcher.agent_runtime.run_autonomous(
                self.dispatcher, event,
                task_context="\u5f53\u524d\u76ee\u6807ID={}\uff1b\u5df2\u6709\u8fdb\u5ea6={}".format(
                    goal.get("id"), goal.get("progress", "")),
                # A worker-triggered review must not queue new background
                # tasks itself (review -> task -> review self-amplifying loop).
                allow_background_queue=False)
            reply = str(plan.get("reply") or "").strip()
            if not reply:
                self.dispatcher.agent_runtime.store.write(
                    "worker/owner_goal_review.json",
                    {"last_run": now, "goal_id": goal.get("id"), "status": "empty", "run_id": run_id},
                )
                return "empty"
            await self.dispatcher.client.send_private_msg(
                owner_id, "\u76ee\u6807\u4e3b\u52a8\u590d\u76d8\uff1a\n{}".format(reply)[:3500])
            self.dispatcher.agent_runtime.goals.update(
                scope_key, goal["id"], progress=reply[:1000])
            self.dispatcher.agent_runtime.proactive.record(
                self.dispatcher.config, scope_key, topic="goal-review", now=now)
            self.dispatcher.agent_runtime.store.write(
                "worker/owner_goal_review.json", {
                    "last_run": now,
                    "goal_id": goal.get("id"),
                    "status": "sent",
                    "run_id": run_id,
                    "tool_results": results[-4:],
                })
            return "sent"
        except Exception as error:
            self.dispatcher.agent_runtime.store.write(
                "worker/owner_goal_review.json",
                {"last_run": now, "goal_id": goal.get("id"), "status": "failed", "run_id": run_id},
            )
            log.exception("Owner goal review failed goal=%s: %s", goal.get("id"), error)
            return "failed"

    async def _review_group_scope(self):
        settings = self.dispatcher.config.get("agent", {})
        if not settings.get("proactive_enabled", True):
            return "disabled"
        groups = self.dispatcher.config.get("groups", {})
        runtime = self.dispatcher.agent_runtime
        now = time.time()
        interval = max(1800, int(settings.get("group_review_interval_seconds", 10800)))
        state = runtime.store.read("worker/group_reviews.json", {})
        if not isinstance(state, dict):
            state = {}
        lease_seconds = self._review_lease_seconds(settings)
        for group_id, group_config in groups.items():
            agent = group_config.get("agent", {}) if isinstance(group_config, dict) else {}
            if not agent.get("proactive_enabled", False):
                continue
            scope_key = "group:{}".format(group_id)
            profile = runtime.profiles.get(scope_key)
            goals = runtime.goals.list(scope_key)
            plans = runtime.plans.list(scope_key, statuses={"active", "running"})
            topics = profile.get("proactive_topics", []) if profile else []
            if not goals and not plans and not topics:
                continue
            group_state = state.get(str(group_id)) or {}
            if self._review_in_progress(group_state, now, lease_seconds):
                continue
            last_run = float(group_state.get("last_run", 0) or 0)
            if now - last_run < interval:
                continue
            topic = "group-review:{}".format(group_id)
            allowed, reason = runtime.proactive.allowed(
                self.dispatcher.config, scope_key, topic=topic,
                is_private=False, now=now)
            if not allowed:
                continue
            owner_id = int(self.dispatcher.config.get("bot_owner") or 0)
            run_id = new_record_id(16)
            state[str(group_id)] = {"status": "running", "run_id": run_id, "started_at": now}
            runtime.store.write("worker/group_reviews.json", state)
            event = runtime.build_event({
                "user_id": owner_id,
                "group_id": int(group_id),
                "message_type": "group",
                "sender": {"role": "owner"},
                "raw_message": "请根据本群画像、目标、计划和最近事件，主动提出一个具体、有用且不过度打扰的推进。",
                "time": now,
            })
            try:
                plan, results = await runtime.run_autonomous(
                    self.dispatcher, event,
                    task_context="这是群域主动复盘。不得泄露其他群或最高主人私域；没有明确价值就保持安静。",
                    allow_background_queue=False,
                    read_only_tools=True,
                )
                if plan.get("needs_confirmation", False):
                    state[str(group_id)] = {"last_run": now, "status": "confirmation_required", "run_id": run_id}
                    runtime.store.write("worker/group_reviews.json", state)
                    return "confirmation_required"
                reply = str(plan.get("reply") or "").strip()
                if not reply:
                    state[str(group_id)] = {"last_run": now, "status": "empty", "run_id": run_id}
                    runtime.store.write("worker/group_reviews.json", state)
                    return "empty"
                await self.dispatcher.client.send_group_msg(int(group_id), reply[:3500])
                runtime.proactive.record(self.dispatcher.config, scope_key, topic=topic, now=now)
                runtime.timeline.add(
                    scope_key, "proactive_group_review", reply[:1000],
                    actor_id=owner_id, evidence=json.dumps(results[-4:], ensure_ascii=False),
                    metadata={"group_id": int(group_id)})
                state[str(group_id)] = {"last_run": now, "status": "sent", "run_id": run_id}
                runtime.store.write("worker/group_reviews.json", state)
                return "sent"
            except Exception as error:
                state[str(group_id)] = {"last_run": now, "status": "failed", "run_id": run_id}
                runtime.store.write("worker/group_reviews.json", state)
                log.exception("Group review failed group=%s: %s", group_id, error)
                return "failed"
        return "idle"

    async def tick(self):
        delivered, failed = await self._deliver_reminders()
        companion_delivered, companion_failed = await self._deliver_companion_outbox()
        task_status = await self._run_owner_task()
        goal_review = await self._review_owner_goal()
        group_review = await self._review_group_scope()
        companion_status = await self._run_owner_companion()
        self.dispatcher.agent_runtime.store.write("worker/status.json", {
            "running": True,
            "mode": "observation" if self.dispatcher.config.get("agent", {}).get("observation_only", True) else "active",
            "last_tick": time.time(),
            "delivered": delivered,
            "failed": failed,
            "companion_delivered": companion_delivered,
            "companion_failed": companion_failed,
            "companion_status": companion_status,
            "task_status": task_status,
            "goal_review": goal_review,
            "group_review": group_review,
        })
