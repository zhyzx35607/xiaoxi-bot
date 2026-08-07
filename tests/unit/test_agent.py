import asyncio
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

class AgentPlannerFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unstructured_planner_output_never_leaks_internal_text(self):
        from bot.agent.planner import AgentPlanner

        runtime = AgentRuntime({"bot_owner": 100, "agent": {}}, tempfile.mkdtemp())
        class Client:
            session = None
        dispatcher = type("Dispatcher", (), {
            "config": runtime.config, "client": Client(),
        })()
        event = runtime.build_event({
            "user_id": 100, "message_type": "private",
            "raw_message": "过来，让我摸摸头",
        })
        internal = (
            "- upload_private_file: NapCat 只读能力\n"
            "作为规划器，我需要判断意图，不需要调用工具。"
        )
        with patch(
            "bot.agent.planner._call_deepseek",
            new=AsyncMock(side_effect=[internal, "主人，我在呢。"]),
        ):
            plan = await AgentPlanner(dispatcher).plan(event)

        self.assertEqual(plan["reply"], "主人，我在呢。")
        self.assertNotIn("upload_private_file", plan["reply"])
        self.assertFalse(plan["needs_confirmation"])
        self.assertEqual(plan["reason"], "unstructured_planner_output")

    async def test_failed_safe_fallback_uses_fixed_reply(self):
        from bot.agent.planner import AgentPlanner

        runtime = AgentRuntime({"bot_owner": 100, "agent": {}}, tempfile.mkdtemp())
        class Client:
            session = None
        dispatcher = type("Dispatcher", (), {
            "config": runtime.config, "client": Client(),
        })()
        event = runtime.build_event({
            "user_id": 100, "message_type": "private",
            "raw_message": "你好",
        })
        with patch(
            "bot.agent.planner._call_deepseek",
            new=AsyncMock(side_effect=["内部规划文本", "execution_plan 不应泄漏"]),
        ):
            plan = await AgentPlanner(dispatcher).plan(event)

        self.assertEqual(plan["reply"], "主人，我刚才没能正常理解这条消息，可以再说一次吗？")
        self.assertNotIn("execution_plan", plan["reply"])


class AgentVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_success_criteria_builds_json_prompt(self):
        from bot.agent.verifier import AgentVerifier

        class Client:
            session = None

        dispatcher = type("Dispatcher", (), {
            "config": {},
            "client": Client(),
        })()
        response = '{"success": true, "reason": "met", "evidence": "tool ok"}'
        with patch("bot.agent.verifier._call_deepseek", new=AsyncMock(return_value=response)) as call:
            result = await AgentVerifier(dispatcher).verify(
                {"goal": "check service", "success_criteria": "service is active"},
                {"reply": "service is active"},
                [{"name": "status", "result": {"ok": True}}],
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "met")
        prompt = call.await_args.args[1][0]["content"]
        self.assertIn("{success:boolean, reason:string, evidence:string}", prompt)
        self.assertIn("service is active", prompt)


class AgentPersistenceTests(unittest.TestCase):
    def test_json_store_serializes_read_modify_write_and_redacts_values(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        threads = [
            threading.Thread(
                target=runtime.goals.create,
                args=("owner:100", 100, f"goal-{index}"),
            )
            for index in range(40)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        records = runtime.goals.list("owner:100", include_done=True)
        self.assertEqual(len(records), 40)

        runtime.store.write("secret.json", {
            "token": "super-secret",
            "nested": "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "ordinary": "tokenizer",
        })
        saved = json.dumps(runtime.store.read("secret.json", {}), ensure_ascii=False)
        self.assertNotIn("super-secret", saved)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", saved)
        self.assertIn("tokenizer", saved)

        runtime.store.write("bounded.json", [{"index": index} for index in range(250)])
        self.assertEqual(len(runtime.store.read("bounded.json", [])), 250)

        legacy_path = Path(runtime.store._path("legacy.json"))
        legacy_path.write_text(json.dumps({
            "message": "Authorization: Bearer legacy-agent-secret",
        }), encoding="utf-8")
        loaded = runtime.store.read("legacy.json", {})
        rewritten = legacy_path.read_text(encoding="utf-8")
        self.assertNotIn("legacy-agent-secret", str(loaded))
        self.assertNotIn("legacy-agent-secret", rewritten)

    def test_persistent_agent_fields_keep_their_declared_lengths(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        plan = runtime.plans.create(
            "owner:100", 100, "题" * 1000,
            [{"title": "步" * 500, "success_criteria": "准" * 500}],
            success_criteria="总" * 1000,
        )
        updated = runtime.plans.update_step(
            "owner:100", plan["id"], "s1", "done",
            evidence="证" * 2000, result="果" * 2000,
        )

        self.assertEqual(len(updated["title"]), 1000)
        self.assertEqual(len(updated["success_criteria"]), 1000)
        self.assertEqual(len(updated["steps"][0]["title"]), 500)
        self.assertEqual(len(updated["steps"][0]["success_criteria"]), 500)
        self.assertEqual(len(updated["steps"][0]["evidence"]), 2000)
        self.assertEqual(len(updated["steps"][0]["result"]), 2000)

    def test_private_and_group_memory_are_isolated(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        private = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "我喜欢咖啡"})
        group = runtime.build_event({"user_id": 100, "group_id": 300, "message_type": "group", "raw_message": "我喜欢茶", "sender": {"role": "owner"}})
        runtime.memory.add_candidate(runtime.extract_memory_candidate(private))
        runtime.memory.add_candidate(runtime.extract_memory_candidate(group))
        self.assertEqual(len(runtime.memory.list_records(private.scope.key, confirmed=True)), 1)
        self.assertEqual(len(runtime.memory.list_records(group.scope.key, confirmed=False)), 1)
        self.assertNotEqual(private.scope.key, group.scope.key)

    def test_memory_rejects_secrets_and_deduplicates(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        secret = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "记住我的 API key 是 sk-secret"})
        self.assertIsNone(runtime.extract_memory_candidate(secret))
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "我偏好简短回答"})
        candidate = runtime.extract_memory_candidate(event)
        runtime.memory.add_candidate(candidate)
        runtime.memory.add_candidate(candidate)
        self.assertEqual(len(runtime.memory.list_records("owner:100", confirmed=True)), 1)

    def test_event_history_redacts_sensitive_message_body(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        runtime.observe({
            "user_id": 100, "message_type": "private",
            "raw_message": "请记住 token=super-secret-value",
        })
        events = runtime.store.read("events/owner_100.json", [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "[敏感内容已省略]")
        self.assertNotIn("super-secret-value", str(events))

    def test_group_rule_is_confirmed_but_member_preference_is_pending(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        rule = runtime.build_event({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "本群以后默认用简短播报", "sender": {"role": "owner"}})
        preference = runtime.build_event({"user_id": 102, "group_id": 300, "message_type": "group", "raw_message": "我喜欢长文", "sender": {"role": "member"}})
        self.assertFalse(runtime.extract_memory_candidate(rule).requires_confirmation)
        self.assertTrue(runtime.extract_memory_candidate(preference).requires_confirmation)

    def test_proactive_budget_and_mute(self):
        runtime = AgentRuntime({"agent": {"quiet_start": 0, "quiet_end": 0}}, tempfile.mkdtemp())
        allowed, reason = runtime.proactive.allowed(runtime.config, "group:300", topic="daily")
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")
        runtime.proactive.record(runtime.config, "group:300", topic="daily", now=1000)
        self.assertEqual(runtime.proactive.allowed(runtime.config, "group:300", topic="daily", now=1100)[1], "topic_cooldown")
        runtime.proactive.mute("group:300", seconds=1000, now=1000)
        self.assertEqual(runtime.proactive.allowed(runtime.config, "group:300", now=1100)[1], "muted")

    def test_owner_rejection_mutes_scope_and_resume_clears_it(self):
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"rejection_mute_seconds": 43200}}, tempfile.mkdtemp())
        runtime.observe({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "别主动了，安静点", "time": 1000, "sender": {"role": "owner"}})
        self.assertGreater(runtime.proactive.muted_until("group:300"), 1000)
        runtime.observe({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "恢复主动", "time": 1100, "sender": {"role": "owner"}})
        self.assertEqual(runtime.proactive.muted_until("group:300"), 0)

    def test_member_rejection_does_not_mute_group_agent(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        runtime.observe({"user_id": 102, "group_id": 300, "message_type": "group", "raw_message": "别主动了", "time": 1000, "sender": {"role": "member"}})
        self.assertEqual(runtime.proactive.muted_until("group:300"), 0)


class ConfirmationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_dispatcher_startup_prunes_expired_confirmations(self):
        from bot.dispatcher import Dispatcher
        from bot.services import confirmations

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending_actions.json"
            path.write_text(json.dumps({
                "expired": {"expires_at": time.time() - 1},
                "valid": {"expires_at": time.time() + 60},
            }), encoding="utf-8")
            with patch.object(confirmations, "_PATH", str(path)), \
                    patch("bot.dispatcher.AgentRuntime"), \
                    patch("bot.dispatcher.AgentWorker"), \
                    patch("bot.dispatcher.RoleplayService"):
                Dispatcher({"runtime": {}}, object())

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("expired", saved)
            self.assertIn("valid", saved)

    async def test_confirmation_rechecks_group_after_permission_lookup(self):
        from bot.permission import LEVEL_ADMIN
        from bot.services import confirmations

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending_actions.json"
            with patch.object(confirmations, "_PATH", str(path)):
                code = confirmations.create_confirmation(
                    100, 7, "set_group_name", {"group_id": 100}, "rename")

                async def move_confirmation(*_args, **_kwargs):
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data[code]["group_id"] = 200
                    path.write_text(json.dumps(data), encoding="utf-8")
                    return LEVEL_ADMIN, "admin"

                client = type("Client", (), {})()
                client.call = AsyncMock()
                dispatcher = type("Dispatcher", (), {"client": client})()
                with patch("bot.permission.get_user_level", side_effect=move_confirmation):
                    ok, message = await confirmations.execute_confirmation(
                        dispatcher, code, 7, 100, "admin")

            self.assertFalse(ok)
            self.assertIn("不属于当前群", message)
            client.call.assert_not_awaited()


class AgentObservationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_passive_message(self, observation_enabled):
        from bot.events.message import GroupMessageMixin

        config = {
            "bot_owner": 100,
            "bot_qq": 200,
            "agent": {"observation_enabled": observation_enabled},
        }
        with tempfile.TemporaryDirectory() as root:
            runtime = AgentRuntime(config, root)

            class Harness(GroupMessageMixin):
                def __init__(self):
                    self.config = config
                    self.agent_runtime = runtime
                    self._lock = asyncio.Lock()
                    self._seen_msg_ids = {}
                    self._seen_msg_ids_maxlen = 10

                async def _is_directed_at_bot(self, message, raw_message=""):
                    return False

            event = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 300,
                "user_id": 101,
                "message_id": 501,
                "time": 1_000,
                "raw_message": "普通测试消息",
                "message": [{"type": "text", "data": {"text": "普通测试消息"}}],
                "sender": {"role": "member", "nickname": "测试用户"},
            }
            with patch("bot.events.message._event_scope_allowed", return_value=True),                     patch("bot.events.message.is_group_enabled", return_value=False):
                await Harness()._handle_message(event)
            return (
                runtime.store.read("events/group_300.json", []),
                runtime.timeline.list("group:300", limit=100),
            )

    async def test_passive_observation_is_disabled_by_default(self):
        events, timeline = await self._run_passive_message(False)
        self.assertEqual(events, [])
        self.assertEqual(timeline, [])

    async def test_passive_observation_is_opt_in_without_message_timeline_duplication(self):
        events, timeline = await self._run_passive_message(True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "普通测试消息")
        self.assertFalse(any(item.get("kind") == "message" for item in timeline))


class AgentToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_exception_does_not_expose_credentials(self):
        from bot.agent.tools.gateway import AgentToolGateway

        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "x"})

        def broken(_dispatcher):
            raise RuntimeError("Authorization: Bearer private-tool-secret")

        dispatcher = type("Dispatcher", (), {"config": {}})()
        gateway = AgentToolGateway(dispatcher)
        gateway._registry = {"uapi_broken": broken}
        result = await gateway.execute(event, "uapi_broken")

        self.assertFalse(result["ok"])
        self.assertNotIn("private-tool-secret", result["message"])
        self.assertIn("[已隐藏]", result["message"])

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

    async def test_registry_read_tool_forces_current_group_scope(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        class Client:
            def __init__(self): self.calls = []
            async def get_group_info(self, group_id):
                self.calls.append(group_id); return {"status": "ok", "data": {"group_id": group_id}}
        client = Client()
        dispatcher = type("D", (), {"config": {}, "agent_runtime": runtime, "client": client})()
        event = runtime.build_event({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "x", "sender": {"role": "owner"}})
        result = await AgentToolGateway(dispatcher).execute(event, "get_group_info", group_id=999)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [300])

    async def test_registry_alias_forces_current_group_scope(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        class Client:
            def __init__(self): self.calls = []
            async def get_group_msg_history(self, group_id, count=20):
                self.calls.append((group_id, count))
                return {"status": "ok", "data": {"messages": []}}
        client = Client()
        dispatcher = type("D", (), {"config": {}, "agent_runtime": runtime, "client": client})()
        event = runtime.build_event({"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "x", "sender": {"role": "owner"}})
        result = await AgentToolGateway(dispatcher).execute(
            event, "get_recent_messages", group_id=999, count=5)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [(300, 5)])

    def test_mutating_napcat_actions_are_not_agent_tools(self):
        from api_registry import REGISTRY
        from bot.agent.tools.gateway import AgentToolGateway
        from bot.agent.tools.napcat import SAFE_ACTIONS
        gateway = AgentToolGateway(type("D", (), {"config": {}})())
        for name in (
            "send_group_msg_reply", "send_group_msg_with_at", "send_flash_msg",
            "click_inline_keyboard_button", "_send_group_notice", "_del_group_notice",
        ):
            with self.subTest(name=name):
                self.assertNotEqual(REGISTRY[name].risk, "read")
                self.assertFalse(REGISTRY[name].ai_allowed)
                self.assertNotIn(name, SAFE_ACTIONS)
                self.assertNotIn(name, gateway.catalog())
                self.assertFalse(gateway.is_read_only(name))

    async def test_owner_private_group_read_requires_explicit_target(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        class Client:
            async def get_group_info(self, group_id):
                return {"status": "ok", "data": {"group_id": group_id}}
        dispatcher = type("D", (), {"config": {}, "agent_runtime": runtime, "client": Client()})()
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "x"})
        denied = await AgentToolGateway(dispatcher).execute(event, "get_group_info")
        allowed = await AgentToolGateway(dispatcher).execute(event, "get_group_info", group_id=300)
        self.assertFalse(denied["ok"])
        self.assertEqual(allowed["data"]["group_id"], 300)

class AgentCommandPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_command_is_group_owner_scoped(self):
        from bot.permission import check_permission
        dispatcher = type("Dispatcher", (), {"config": {"bot_owner": 999, "bot_qq": 888}})()
        allowed, error = await check_permission(dispatcher, 300, 101, "member", {"owner_only": True})
        self.assertFalse(allowed)
        self.assertIn("群主", error)
        allowed, error = await check_permission(dispatcher, 300, 102, "owner", {"owner_only": True})
        self.assertTrue(allowed)

    async def test_owner_private_agent_command_uses_registered_router(self):
        from bot.events.message import PrivateMessageMixin

        calls = []

        class Dispatcher(PrivateMessageMixin):
            config = {"bot_owner": 100}
            commands = {"agent": {"handler": object()}}

            async def _run_command(self, *args):
                calls.append(args)

            async def _reply(self, *args):
                raise AssertionError("registered command must not be reported as unknown")

        message = [{"type": "text", "data": {"text": "/agent"}}]
        await Dispatcher()._handle_owner_command(
            "agent", "", 100, {"nickname": "owner"}, message, "/agent",
        )

        self.assertEqual(calls[0][0], "agent")
        self.assertEqual(calls[0][2], None)
        self.assertEqual(calls[0][3], 100)


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

    async def test_group_router_can_run_while_owner_router_is_in_observation_mode(self):
        config = {
            "bot_owner": 100,
            "agent": {"primary_router": False, "observation_only": True, "owner_autonomy_enabled": False},
            "groups": {"300": {"agent": {"primary_router": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        calls = []
        class Planner:
            async def plan(self, agent_event, context):
                return {"reply": "群域回复", "tools": [], "needs_confirmation": False, "intent": "group", "task": None}
        runtime.planner = Planner()
        runtime.tools = type("Tools", (), {"catalog": lambda self: {}})()
        runtime.executor = object()
        runtime.verifier = object()
        async def run(*args, **kwargs):
            calls.append(True)
            return {"reply": "群域回复", "tools": [], "needs_confirmation": False, "intent": "group"}, []
        runtime.run_autonomous = run
        class Client:
            def __init__(self): self.sent = []
            async def send_group_msg_with_at(self, group_id, text, users):
                self.sent.append((group_id, text, users)); return {"status": "ok"}
        dispatcher = type("D", (), {"config": config, "client": Client()})()
        event = {"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "推进群目标", "sender": {"role": "owner"}}
        self.assertTrue(runtime.primary_router_enabled(event))
        self.assertTrue(await runtime.handle_event(dispatcher, event, explicit=True))
        self.assertEqual(len(calls), 1)
        self.assertEqual(dispatcher.client.sent[0][0], 300)

    def test_private_owner_router_remains_independently_disabled(self):
        config = {
            "bot_owner": 100,
            "agent": {"primary_router": False, "observation_only": True, "owner_autonomy_enabled": False},
            "groups": {"300": {"agent": {"primary_router": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        self.assertFalse(runtime.primary_router_enabled({"user_id": 100, "message_type": "private", "raw_message": "x"}))

    async def test_background_worker_marks_verified_task_done(self):
        from bot.agent.worker_service import AgentWorker
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"owner_autonomy_enabled": True, "background_tasks_enabled": True}}, tempfile.mkdtemp())
        runtime.tasks.create("owner:100", 100, "做事", success_criteria="完成")
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

    async def test_background_worker_does_not_requeue_when_completion_notice_fails(self):
        from bot.agent.worker_service import AgentWorker
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"owner_autonomy_enabled": True, "background_tasks_enabled": True}}, tempfile.mkdtemp())
        runtime.tasks.create("owner:100", 100, "做一次性操作", success_criteria="完成")
        runtime.execute_background_task = AsyncMock(return_value={"success": True, "reply": "完成"})

        class Client:
            async def send_private_msg(self, user_id, text):
                raise RuntimeError("onebot unavailable")

        dispatcher = type("D", (), {"config": runtime.config, "agent_runtime": runtime, "client": Client()})()
        status = await AgentWorker(dispatcher)._run_owner_task()
        self.assertEqual(status, "done")
        self.assertEqual(runtime.tasks.list("owner:100")[0]["status"], "done")

    async def test_worker_survives_first_tick_failure(self):
        from bot.agent.worker_service import AgentWorker
        config = {"agent": {"worker_interval_seconds": 10}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": object()})()
        worker = AgentWorker(dispatcher)
        calls = []

        async def failing_tick():
            calls.append(True)
            worker._stop.set()
            raise RuntimeError("transient")

        worker.tick = failing_tick
        await worker._run()
        self.assertEqual(calls, [True])

    def test_stale_running_task_is_recovered(self):
        runtime = AgentRuntime({"agent": {}}, tempfile.mkdtemp())
        task = runtime.tasks.create("owner:100", 100, "恢复任务")
        runtime.tasks.update(task["id"], "running")
        records = runtime.store.read("tasks/index.json", [])
        records[0]["updated_at"] = 1
        runtime.store.write("tasks/index.json", records)
        queued = runtime.tasks.next_queued(stale_after_seconds=60)
        self.assertEqual(queued["id"], task["id"])
        self.assertEqual(runtime.tasks.list("owner:100")[0]["status"], "queued")

class AgentGoalReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_goal_review_sends_and_updates_progress(self):
        from bot.agent.worker_service import AgentWorker
        config = {"bot_owner": 100, "agent": {"owner_autonomy_enabled": True, "quiet_start": 0, "quiet_end": 0, "owner_goal_check_interval_seconds": 1800}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        goal = runtime.goals.create("owner:100", 100, "持续完善机器人")
        calls = []
        async def run(dispatcher, event, task_context="", allow_background_queue=True, **kwargs):
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

    async def test_goal_review_running_lease_blocks_duplicate_run(self):
        from bot.agent.worker_service import AgentWorker
        config = {"bot_owner": 100, "agent": {"owner_autonomy_enabled": True, "review_lease_seconds": 3600}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.goals.create("owner:100", 100, "只执行一次")
        runtime.store.write("worker/owner_goal_review.json", {
            "status": "running", "started_at": time.time(), "run_id": "in-flight",
        })
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": object()})()
        self.assertEqual(await AgentWorker(dispatcher)._review_owner_goal(), "in_progress")

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


class AgentPlanStateTests(unittest.TestCase):
    def test_plan_steps_drive_terminal_status_and_require_real_step(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        plan = runtime.plans.create(
            "owner:100", 100, "完善机器人",
            [{"title": "检查", "success_criteria": "有报告"}, {"title": "修复"}])
        self.assertIsNone(runtime.plans.update_step("owner:100", plan["id"], "missing", "done"))
        runtime.plans.update_step("owner:100", plan["id"], "s1", "done", evidence="报告")
        current = runtime.plans.get("owner:100", plan["id"])
        self.assertEqual(current["status"], "active")
        runtime.plans.update_step("owner:100", plan["id"], "s2", "done", evidence="测试通过")
        self.assertEqual(runtime.plans.get("owner:100", plan["id"])["status"], "done")

    def test_profile_skill_insight_and_timeline_are_scope_isolated(self):
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        runtime.profiles.update("group:1", persona="活泼的群助手", proactive_topics=["新番"])
        runtime.skills.create("group:1", 101, "新番播报", "先查证再总结", triggers=["新番"])
        runtime.insights.add("group:1", "群友偏好简短播报", evidence="三次反馈")
        runtime.timeline.add("group:1", "test", "完成一次播报")
        event = runtime.build_event({"user_id": 101, "group_id": 1, "message_type": "group", "raw_message": "聊聊新番", "sender": {"role": "owner"}})
        context = runtime.context.build(event)
        self.assertIn("活泼的群助手", context)
        self.assertIn("先查证再总结", context)
        self.assertIn("群友偏好简短播报", context)
        self.assertEqual(runtime.profiles.get("group:2"), {})
        self.assertEqual(runtime.skills.list("group:2"), [])


class AgentAdvancedNativeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_cannot_use_native_write_tools(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        dispatcher = type("D", (), {"config": {}, "agent_runtime": runtime})()
        event = runtime.build_event({"user_id": 200, "group_id": 1, "message_type": "group", "raw_message": "x", "sender": {"role": "member"}})
        result = await AgentToolGateway(dispatcher).execute(event, "agent_create_plan", title="越权", steps=["一步"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "agent_write_requires_owner")
        self.assertEqual(runtime.plans.list("group:1"), [])

    async def test_owner_can_create_plan_and_update_bound_step(self):
        from bot.agent.tools.gateway import AgentToolGateway
        runtime = AgentRuntime({"bot_owner": 100}, tempfile.mkdtemp())
        dispatcher = type("D", (), {"config": {}, "agent_runtime": runtime})()
        event = runtime.build_event({"user_id": 101, "group_id": 1, "message_type": "group", "raw_message": "x", "sender": {"role": "owner"}})
        gateway = AgentToolGateway(dispatcher)
        created = await gateway.execute(event, "agent_create_plan", title="群计划", steps=["检查", "汇报"])
        plan_id = created["data"]["id"]
        updated = await gateway.execute(event, "agent_update_plan_step", plan_id=plan_id, step_id="s1", status="done", evidence="已检查")
        self.assertTrue(updated["ok"])
        self.assertEqual(runtime.plans.get("group:1", plan_id)["steps"][0]["status"], "done")


class AgentExecutionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_autonomous_plan_persists_tool_evidence_and_reflection(self):
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"owner_max_rounds": 1, "owner_tool_budget": 2}}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "message_type": "private", "raw_message": "检查状态"})
        class Planner:
            async def plan(self, agent_event, context):
                return {
                    "intent": "audit", "reply": "检查完成", "needs_confirmation": False,
                    "tools": [{"name": "fake_read", "arguments": {}, "step_id": "s1"}],
                    "execution_plan": {"title": "状态检查", "success_criteria": "有证据", "steps": [{"title": "读取状态"}]},
                    "reflection": {"content": "状态正常", "category": "health", "confidence": 0.9, "evidence": "工具返回 ok"},
                    "task": None,
                }
        class Executor:
            async def execute(self, agent_event, calls, remaining_budget):
                return [{"name": "fake_read", "step_id": "s1", "arguments": {}, "result": {"ok": True, "data": "healthy"}}]
        runtime.planner = Planner()
        runtime.tools = type("Tools", (), {"catalog": lambda self: {"fake_read": "read"}})()
        runtime.executor = Executor()
        dispatcher = type("D", (), {})()
        plan, results = await runtime.run_autonomous(dispatcher, event)
        saved = runtime.plans.get("owner:100", plan["plan_id"])
        self.assertEqual(saved["steps"][0]["status"], "done")
        self.assertIn("fake_read", saved["steps"][0]["evidence"])
        self.assertEqual(runtime.insights.list("owner:100")[0]["content"], "状态正常")
        self.assertTrue(any(item["kind"] == "tool_result" for item in runtime.timeline.list("owner:100")))

    async def test_group_confirmation_does_not_execute_tools_before_approval(self):
        config = {
            "bot_owner": 100,
            "agent": {"primary_router": False, "observation_only": True},
            "groups": {"300": {"agent": {"primary_router": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        executions = []
        class Planner:
            async def plan(self, agent_event, context):
                return {
                    "intent": "sensitive", "reply": "准备执行", "needs_confirmation": True,
                    "reason": "需要群主确认", "tools": [{"name": "fake_write", "arguments": {}}],
                    "task": None, "execution_plan": None, "reflection": None,
                }
        class Executor:
            async def execute(self, *args, **kwargs):
                executions.append(True)
                return []
        runtime.planner = Planner()
        runtime.tools = type("Tools", (), {"catalog": lambda self: {"fake_write": "write"}})()
        runtime.executor = Executor()
        class Client:
            def __init__(self): self.sent = []
            async def send_group_msg_with_at(self, group_id, text, users):
                self.sent.append((group_id, text, users)); return {"status": "ok"}
        dispatcher = type("D", (), {"client": Client()})()
        event = {"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "执行方案", "sender": {"role": "owner"}}
        with patch("bot.services.confirmations.create_agent_confirmation", return_value="abc123"):
            self.assertTrue(await runtime.handle_event(dispatcher, event, explicit=True))
        self.assertEqual(executions, [])
        self.assertIn("/确认 abc123", dispatcher.client.sent[0][1])

    async def test_confirmed_plan_executes_only_frozen_tool_set(self):
        config = {"bot_owner": 100, "agent": {"group_max_rounds": 2, "group_tool_budget": 5}, "groups": {"300": {"agent": {"primary_router": True}}}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        executed = []
        class Planner:
            async def plan(self, agent_event, context):
                return {"intent": "summary", "reply": "执行完成", "needs_confirmation": False, "tools": [{"name": "new_unapproved", "arguments": {}}], "task": None}
        class Executor:
            async def execute(self, agent_event, calls, remaining_budget):
                executed.extend(item["name"] for item in calls)
                return [{"name": item["name"], "result": {"ok": True}} for item in calls]
        runtime.planner = Planner()
        runtime.tools = type("Tools", (), {"catalog": lambda self: {}})()
        runtime.executor = Executor()
        class Client:
            def __init__(self): self.sent = []
            async def send_group_msg_with_at(self, group_id, text, users):
                self.sent.append((group_id, text, users)); return {"status": "ok"}
        dispatcher = type("D", (), {"client": Client()})()
        frozen = {"intent": "approved", "reply": "", "needs_confirmation": True, "tools": [{"name": "approved_tool", "arguments": {}}], "task": None}
        result = await runtime.execute_confirmed_plan(dispatcher, {"user_id": 101, "group_id": 300, "message_type": "group", "raw_message": "x", "sender": {"role": "owner"}}, frozen, role="owner")
        self.assertTrue(result["success"])
        self.assertEqual(executed, ["approved_tool"])


class AgentGroupProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_autonomy_filters_native_write_tools(self):
        runtime = AgentRuntime({"bot_owner": 100, "agent": {"group_max_rounds": 1, "group_tool_budget": 5}}, tempfile.mkdtemp())
        event = runtime.build_event({"user_id": 100, "group_id": 300, "message_type": "group", "raw_message": "主动复盘", "sender": {"role": "owner"}})
        class Planner:
            async def plan(self, agent_event, context):
                return {"reply": "只读完成", "needs_confirmation": False, "intent": "review", "task": None, "tools": [
                    {"name": "agent_create_goal", "arguments": {"title": "不应创建"}},
                    {"name": "get_group_info", "arguments": {}},
                ]}
        class Tools:
            def catalog(self): return {}
            def is_read_only(self, name): return name != "agent_create_goal"
        captured = []
        class Executor:
            async def execute(self, agent_event, calls, remaining_budget):
                captured.extend(item["name"] for item in calls)
                return []
        runtime.planner = Planner()
        runtime.tools = Tools()
        runtime.executor = Executor()
        runtime.verifier = object()
        await runtime.run_autonomous(type("D", (), {})(), event, read_only_tools=True)
        self.assertEqual(captured, ["get_group_info"])

    async def test_group_review_requires_opt_in_and_uses_group_scope(self):
        from bot.agent.worker_service import AgentWorker
        config = {
            "bot_owner": 100,
            "agent": {"proactive_enabled": True, "quiet_start": 0, "quiet_end": 0, "topic_cooldown_seconds": 1800, "group_review_interval_seconds": 1800},
            "groups": {"300": {"agent": {"proactive_enabled": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.profiles.update("group:300", persona="群助手", proactive_topics=["新番"])
        calls = []
        async def run(dispatcher, event, task_context="", allow_background_queue=True, **kwargs):
            calls.append((event.scope.key, task_context))
            return {"reply": "今天可以一起整理新番表", "tools": [], "needs_confirmation": False}, []
        runtime.run_autonomous = run
        class Client:
            def __init__(self): self.sent = []
            async def send_group_msg(self, group_id, text): self.sent.append((group_id, text)); return {"status": "ok"}
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": Client()})()
        worker = AgentWorker(dispatcher)
        self.assertEqual(await worker._review_group_scope(), "sent")
        self.assertEqual(calls[0][0], "group:300")
        self.assertEqual(dispatcher.client.sent[0][0], 300)
        self.assertEqual(await worker._review_group_scope(), "idle")

    async def test_group_review_stays_disabled_without_group_authorization(self):
        from bot.agent.worker_service import AgentWorker
        config = {"bot_owner": 100, "agent": {"proactive_enabled": True, "quiet_start": 0, "quiet_end": 0}, "groups": {"300": {"agent": {"proactive_enabled": False}}}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.goals.create("group:300", 101, "不应主动")
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": object()})()
        self.assertEqual(await AgentWorker(dispatcher)._review_group_scope(), "idle")

    async def test_group_review_running_lease_blocks_duplicate_run(self):
        from bot.agent.worker_service import AgentWorker
        config = {
            "bot_owner": 100,
            "agent": {"proactive_enabled": True, "review_lease_seconds": 3600},
            "groups": {"300": {"agent": {"proactive_enabled": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.profiles.update("group:300", persona="群助手", proactive_topics=["新番"])
        runtime.store.write("worker/group_reviews.json", {
            "300": {"status": "running", "started_at": time.time(), "run_id": "in-flight"},
        })
        runtime.run_autonomous = AsyncMock(side_effect=AssertionError("duplicate review"))
        dispatcher = type("D", (), {"config": config, "agent_runtime": runtime, "client": object()})()
        self.assertEqual(await AgentWorker(dispatcher)._review_group_scope(), "idle")
