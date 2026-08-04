import tempfile
import unittest
from datetime import datetime

from bot.agent.identity import resolve_identity, resolve_scope
from bot.agent.models import IdentityLevel
from bot.agent.policy import decide_event, is_quiet_hours, tool_allowed
from bot.agent.runtime import AgentRuntime


class AgentIdentityTests(unittest.TestCase):
    def test_super_owner_overrides_group_role(self):
        config = {"bot_owner": 100, "bot_qq": 200}
        event = {"user_id": 100, "group_id": 300, "message_type": "group", "sender": {"role": "member"}}
        self.assertEqual(resolve_identity(config, event).level, IdentityLevel.SUPER_OWNER)
        self.assertEqual(resolve_scope(config, event).key, "group:300")

    def test_group_owner_is_not_super_owner(self):
        identity = resolve_identity({"bot_owner": 100, "bot_qq": 200}, {"user_id": 101, "message_type": "group", "sender": {"role": "owner"}})
        self.assertEqual(identity.level, IdentityLevel.GROUP_OWNER)
        self.assertFalse(identity.is_super_owner)


class AgentPolicyTests(unittest.TestCase):
    def test_quiet_window_crosses_midnight(self):
        settings = {"quiet_start": 23, "quiet_end": 9}
        self.assertTrue(is_quiet_hours(settings, datetime(2026, 8, 4, 23)))
        self.assertTrue(is_quiet_hours(settings, datetime(2026, 8, 4, 8)))
        self.assertFalse(is_quiet_hours(settings, datetime(2026, 8, 4, 12)))

    def test_member_is_passive_but_owner_can_be_candidate(self):
        base = {"bot_owner": 100, "agent": {"member_passive_only": True, "quiet_start": 0, "quiet_end": 0}}
        member = AgentRuntime(base, tempfile.mkdtemp()).build_event({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "hi", "sender": {"role": "member"}})
        owner = AgentRuntime(base, tempfile.mkdtemp()).build_event({"user_id": 102, "group_id": 300, "message_type": "group", "raw_message": "hi", "sender": {"role": "owner"}})
        self.assertEqual(decide_event(base, member).reason, "member_passive_only")
        self.assertEqual(decide_event(base, owner).reason, "privileged_proactive_candidate")

    def test_sensitive_napcat_actions_are_never_allowed(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "x"})
        self.assertFalse(tool_allowed({}, event, "get_cookies"))
        self.assertTrue(tool_allowed({}, event, "get_group_info"))

class AgentPersistenceTests(unittest.TestCase):
    def test_private_and_group_memory_are_isolated(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        private = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "我喜欢咖啡"})
        group = runtime.build_event({"user_id": 100, "group_id": 300, "message_type": "group", "raw_message": "我喜欢茶", "sender": {"role": "owner"}})
        runtime.memory.add_candidate(runtime.extract_memory_candidate(private))
        runtime.memory.add_candidate(runtime.extract_memory_candidate(group))
        self.assertEqual(len(runtime.memory.list_records(private.scope.key, confirmed=True)), 1)
        self.assertEqual(len(runtime.memory.list_records(group.scope.key, confirmed=True)), 1)
        self.assertNotEqual(private.scope.key, group.scope.key)

    def test_proactive_budget_and_mute(self):
        runtime = AgentRuntime({"agent": {"quiet_start": 0, "quiet_end": 0}}, tempfile.mkdtemp())
        allowed, reason = runtime.proactive.allowed(runtime.config, "group:300", topic="daily")
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")
        runtime.proactive.record(runtime.config, "group:300", topic="daily", now=1000)
        self.assertEqual(runtime.proactive.allowed(runtime.config, "group:300", topic="daily", now=1100)[1], "topic_cooldown")
        runtime.proactive.mute("group:300", seconds=1000, now=1000)
        self.assertEqual(runtime.proactive.allowed(runtime.config, "group:300", now=1100)[1], "muted")


class AgentToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_denies_sensitive_tool_before_registry_lookup(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "x"})
        gateway = AgentToolGateway(type("Dispatcher", (), {"config": {}})())
        result = await gateway.execute(event, "get_cookies")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "tool_denied_by_agent_policy")

    async def test_allowlisted_napcat_read_action(self):
        from bot.agent.tools.gateway import AgentToolGateway
        class Client:
            async def call(self, action, params):
                return {"status": "ok", "data": {"action": action, "params": params}}
        dispatcher = type("Dispatcher", (), {"config": {}, "client": Client()})()
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "x"})
        result = await AgentToolGateway(dispatcher).execute(event, "nc_get_user_status", user_id="123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["params"]["user_id"], 123)

class AgentCommandPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_command_is_group_owner_scoped(self):
        from bot.permission import check_permission
        dispatcher = type("Dispatcher", (), {"config": {"bot_owner": 999, "bot_qq": 888}})()
        allowed, error = await check_permission(dispatcher, 300, 101, "member", {"owner_only": True})
        self.assertFalse(allowed)
        self.assertIn("群主", error)
        allowed, error = await check_permission(dispatcher, 300, 102, "owner", {"owner_only": True})
        self.assertTrue(allowed)

class AgentResponsePolicyTests(unittest.TestCase):
    def test_observation_mode_never_autosends(self):
        from bot.agent.response import can_autosend
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"observation_only": True}}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "安排一下"})
        allowed, reason = can_autosend(runtime.config, event, {"needs_confirmation": False})
        self.assertFalse(allowed)
        self.assertEqual(reason, "observation_only")

    def test_group_owner_requires_confirmation_for_sensitive_plan(self):
        from bot.agent.response import can_autosend
        runtime = AgentRuntime({"agent": {"observation_only": False}}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "安排一下", "sender": {"role": "owner"}})
        allowed, reason = can_autosend(runtime.config, event, {"needs_confirmation": True})
        self.assertFalse(allowed)
        self.assertEqual(reason, "confirmation_required")


class AgentGoalReminderTests(unittest.IsolatedAsyncioTestCase):
    def test_goal_lifecycle(self):
        runtime = AgentRuntime({}, tempfile.mkdtemp())
        goal = runtime.goals.create("owner:100", 100, "完成 Agent 改造")
        self.assertEqual(len(runtime.goals.list("owner:100")), 1)
        runtime.goals.update("owner:100", goal["id"], status="done", progress="已完成")
        self.assertEqual(runtime.goals.list("owner:100"), [])

    async def test_due_reminder_is_delivered_once(self):
        from bot.agent.worker_service import AgentWorker
        class Client:
            def __init__(self):
                self.sent = []
            async def send_private_msg(self, user_id, text):
                self.sent.append((user_id, text))
                return {"status": "ok"}
        runtime = AgentRuntime({"agent": {}}, tempfile.mkdtemp())
        runtime.reminders.create("owner:100", 100, "喝水", 1)
        dispatcher = type("Dispatcher", (), {"config": {"agent": {}}, "agent_runtime": runtime, "client": Client()})()
        worker = AgentWorker(dispatcher)
        await worker.tick()
        self.assertEqual(dispatcher.client.sent, [(100, "提醒你：喝水")])
        self.assertEqual(runtime.reminders.list("owner:100"), [])

class AgentAutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_autonomy_replans_after_tool_result(self):
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"owner_max_rounds": 4, "owner_tool_budget": 4}}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "查一下再回答"})

        class Planner:
            def __init__(self):
                self.calls = 0
            async def plan(self, agent_event, context=""):
                self.calls += 1
                if self.calls == 1:
                    return {"intent": "lookup", "reply": "", "tools": [{"name": "get_group_info", "arguments": {"group_id": 1}}], "needs_confirmation": False, "task": None}
                self_test.assertIn("tool-ok", context)
                return {"intent": "answer", "reply": "完成了", "tools": [], "needs_confirmation": False, "task": None}

        class Executor:
            async def execute(self, agent_event, calls, remaining_budget):
                return [{"name": "get_group_info", "result": {"ok": True, "data": "tool-ok"}}]

        self_test = self
        runtime.planner = Planner()
        runtime.tools = object()
        runtime.executor = Executor()
        runtime.verifier = object()
        plan, results = await runtime.run_autonomous(type("D", (), {})(), event)
        self.assertEqual(plan["reply"], "完成了")
        self.assertEqual(len(results), 1)
        self.assertEqual(runtime.planner.calls, 2)

    async def test_group_router_requires_per_group_authorization(self):
        config = {"bot_owner": 100, "agent": {"primary_router": True, "observation_only": False, "owner_autonomy_enabled": True}, "groups": {"300": {"agent": {"primary_router": False}}}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        async def fail_if_called(*args, **kwargs):
            raise AssertionError("planner must not run")
        runtime.run_autonomous = fail_if_called
        handled = await runtime.handle_event(type("D", (), {})(), {"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "hi", "sender": {"role": "owner"}}, explicit=True)
        self.assertFalse(handled)

    async def test_background_worker_marks_verified_task_done(self):
        from bot.agent.worker_service import AgentWorker
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"owner_autonomy_enabled": True, "background_tasks_enabled": True}}, tempfile.mkdtemp())
        task = runtime.tasks.create("owner:100", 100, "做事", success_criteria="完成")
        async def execute(dispatcher, item):
            return {"success": True, "reply": "任务结果", "reason": "ok"}
        runtime.execute_background_task = execute
        class Client:
            def __init__(self): self.sent = []
            async def send_private_msg(self, user_id, text):
                self.sent.append((user_id, text)); return {"status": "ok"}
        dispatcher = type("D", (), {"config": runtime.config, "agent_runtime": runtime, "client": Client()})()
        status = await AgentWorker(dispatcher)._run_owner_task()
        self.assertEqual(status, "done")
        saved = runtime.tasks.list("owner:100")
        self.assertEqual(saved[0]["status"], "done")
        self.assertEqual(saved[0]["attempts"], 1)
        self.assertIn("任务结果", dispatcher.client.sent[0][1])

class AgentGoalReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_goal_review_sends_and_updates_progress(self):
        from bot.agent.worker_service import AgentWorker
        config = {"bot_owner": 100, "agent": {"owner_autonomy_enabled": True, "quiet_start": 0, "quiet_end": 0, "owner_goal_check_interval_seconds": 1800}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        goal = runtime.goals.create("owner:100", 100, "持续完善机器人")
        calls = []
        async def run(dispatcher, event, task_context="", allow_background_queue=True):
            calls.append((event.text, task_context))
            return {"reply": "下一步补观测指标", "intent": "goal-review", "tools": [], "needs_confirmation": False}, []
        runtime.run_autonomous = run
        class Client:
            def __init__(self): self.sent = []
            async def send_private_msg(self, user_id, text):
                self.sent.append((user_id, text)); return {"status": "ok"}
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": Client()})()
        result = await AgentWorker(dispatcher)._review_owner_goal()
        self.assertEqual(result, "sent")
        self.assertEqual(len(calls), 1)
        self.assertIn("下一步补观测指标", dispatcher.client.sent[0][1])
        updated = runtime.goals.list("owner:100")[0]
        self.assertEqual(updated["id"], goal["id"])
        self.assertEqual(updated["progress"], "下一步补观测指标")
        self.assertEqual(await AgentWorker(dispatcher)._review_owner_goal(), "cooldown")

    async def test_goal_review_is_disabled_without_owner_autonomy(self):
        from bot.agent.worker_service import AgentWorker
        config = {"bot_owner": 100, "agent": {"owner_autonomy_enabled": False}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.goals.create("owner:100", 100, "不会执行")
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": object()})()
        self.assertEqual(await AgentWorker(dispatcher)._review_owner_goal(), "disabled")

class AgentNativeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_goal_tool_is_bound_to_event_scope(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        class Dispatcher:
            config = {}
            agent_runtime = runtime
        gateway = AgentToolGateway(Dispatcher())
        group_event = runtime.build_event({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "x", "sender": {"role": "owner"}})
        result = await gateway.execute(group_event, "agent_create_goal", title="群目标")
        self.assertTrue(result["ok"])
        self.assertEqual(len(runtime.goals.list("group:300")), 1)
        self.assertEqual(runtime.goals.list("owner:100"), [])

    async def test_native_reminder_tool_cannot_choose_another_scope(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        class Dispatcher:
            config = {}
            agent_runtime = runtime
        gateway = AgentToolGateway(Dispatcher())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "x"})
        result = await gateway.execute(event, "agent_create_reminder", text="测试", delay_seconds=60, scope_key="group:999")
        self.assertTrue(result["ok"])
        self.assertEqual(len(runtime.reminders.list("owner:100")), 1)
        self.assertEqual(runtime.reminders.list("group:999"), [])

    def test_gateway_catalog_contains_native_tools(self):
        from bot.agent.tools.gateway import AgentToolGateway
        gateway = AgentToolGateway(type("D", (), {"config": {}})())
        catalog = gateway.catalog()
        self.assertIn("agent_create_goal", catalog)
        self.assertIn("agent_create_reminder", catalog)
