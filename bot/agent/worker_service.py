"""Lifecycle worker for reminders, owner tasks, and goal reviews."""

import asyncio
import json
import logging
import time

log = logging.getLogger("qqbot")


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
        await self.tick()
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

    async def _run_owner_task(self):
        settings = self.dispatcher.config.get("agent", {})
        if not settings.get("owner_autonomy_enabled", False) or not settings.get("background_tasks_enabled", True):
            return "disabled"
        task = self.dispatcher.agent_runtime.tasks.next_queued()
        if not task:
            return "idle"
        self.dispatcher.agent_runtime.tasks.update(task["id"], "running")
        try:
            result = await self.dispatcher.agent_runtime.execute_background_task(
                self.dispatcher, task)
            attempts = int(task.get("attempts", 0)) + 1
            if result.get("success"):
                self.dispatcher.agent_runtime.tasks.update(
                    task["id"], "done", json.dumps(result, ensure_ascii=False))
                summary = "\u540e\u53f0\u4efb\u52a1 {} \u5df2\u5b8c\u6210\n{}".format(
                    task["id"], result.get("reply") or result.get("evidence") or "\u5df2\u901a\u8fc7\u9a8c\u6536")
                await self.dispatcher.client.send_private_msg(
                    int(task["owner_id"]), summary[:3500])
                return "done"
            max_attempts = int(settings.get("background_task_max_attempts", 3))
            status = "failed" if attempts >= max_attempts else "queued"
            self.dispatcher.agent_runtime.tasks.update(
                task["id"], status, json.dumps(result, ensure_ascii=False),
                result.get("reason", ""))
            if status == "failed":
                await self.dispatcher.client.send_private_msg(
                    int(task["owner_id"]),
                    "\u540e\u53f0\u4efb\u52a1 {} \u8fde\u7eed\u5931\u8d25\uff0c\u5df2\u505c\u6b62\uff1a{}".format(
                        task["id"], result.get("reason", "\u672a\u77e5\u539f\u56e0"))[:2000])
            return status
        except Exception as error:
            attempts = int(task.get("attempts", 0)) + 1
            max_attempts = int(settings.get("background_task_max_attempts", 3))
            status = "failed" if attempts >= max_attempts else "queued"
            self.dispatcher.agent_runtime.tasks.update(
                task["id"], status, error=str(error))
            log.exception("Agent background task failed id=%s", task.get("id"))
            return status

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
        last_run = float(state.get("last_run", 0) or 0) if isinstance(state, dict) else 0
        now = time.time()
        if now - last_run < interval:
            return "cooldown"
        allowed, reason = self.dispatcher.agent_runtime.proactive.allowed(
            self.dispatcher.config, scope_key, topic="goal-review",
            is_private=True, now=now)
        if not allowed:
            return reason
        goal = goals[0]
        event = self.dispatcher.agent_runtime.build_event({
            "user_id": owner_id,
            "message_type": "private",
            "raw_message": "\u8bf7\u4e3b\u52a8\u590d\u76d8\u5e76\u63a8\u8fdb\u8fd9\u4e2a\u957f\u671f\u76ee\u6807\uff1a{}".format(
                goal.get("title", "")),
            "time": now,
        })
        plan, results = await self.dispatcher.agent_runtime.run_autonomous(
            self.dispatcher, event,
            task_context="\u5f53\u524d\u76ee\u6807ID={}\uff1b\u5df2\u6709\u8fdb\u5ea6={}".format(
                goal.get("id"), goal.get("progress", "")))
        reply = str(plan.get("reply") or "").strip()
        if not reply:
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
                "tool_results": results[-4:],
            })
        return "sent"

    async def tick(self):
        delivered, failed = await self._deliver_reminders()
        task_status = await self._run_owner_task()
        goal_review = await self._review_owner_goal()
        self.dispatcher.agent_runtime.store.write("worker/status.json", {
            "running": True,
            "mode": "observation" if self.dispatcher.config.get("agent", {}).get("observation_only", True) else "active",
            "last_tick": time.time(),
            "delivered": delivered,
            "failed": failed,
            "task_status": task_status,
            "goal_review": goal_review,
        })
