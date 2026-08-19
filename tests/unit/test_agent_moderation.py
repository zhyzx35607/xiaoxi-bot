import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot.permission as permission_module
from bot.agent.executor import AgentExecutor
from bot.agent.runtime import AgentRuntime, _plan_requires_confirmation
from bot.agent.tools.gateway import AgentToolGateway


class FakeClient:
    def __init__(self, roles=None):
        self.roles = roles or {}
        self.calls = []
        self.sent = []

    async def get_group_member_info(self, group_id, user_id):
        return {"status": "ok", "data": {"role": self.roles.get(user_id, "member")}}

    async def delete_msg(self, message_id):
        self.calls.append(("delete_msg", message_id))
        return {"status": "ok"}

    async def set_group_ban(self, group_id, user_id, duration=1800):
        self.calls.append(("set_group_ban", group_id, user_id, duration))
        return {"status": "ok"}

    async def set_group_kick(self, group_id, user_id, reject_add=False):
        self.calls.append(("set_group_kick", group_id, user_id, reject_add))
        return {"status": "ok"}

    async def set_group_add_request(self, flag, sub_type, approve=True, reason=""):
        self.calls.append(("set_group_add_request", flag, sub_type, approve, reason))
        return {"status": "ok"}

    async def send_group_msg(self, group_id, text):
        self.sent.append((group_id, text))
        return {"status": "ok"}

    async def send_group_msg_with_at(self, group_id, text, users):
        self.sent.append((group_id, text, users))
        return {"status": "ok"}


def make_runtime(config=None):
    base = {"bot_owner": 999, "bot_qq": 888}
    base.update(config or {})
    return AgentRuntime(base, tempfile.mkdtemp())


def make_dispatcher(runtime, client):
    return type("D", (), {
        "config": runtime.config,
        "client": client,
        "agent_runtime": runtime,
    })()


def group_event(runtime, **overrides):
    payload = {
        "user_id": 101, "group_id": 300, "message_type": "group",
        "raw_message": "x", "sender": {"role": "owner"},
    }
    payload.update(overrides)
    return runtime.build_event(payload)


MODERATION_CONFIG = {
    "agent": {"moderation_daily_limit": 20, "moderation_ban_max_seconds": 600},
    "groups": {"300": {"enabled": True, "agent": {"moderation_enabled": True}}},
}


class ModerationToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    async def execute(self, runtime, dispatcher, event, tool, **params):
        return await AgentToolGateway(dispatcher).execute(event, tool, **params)

    async def test_config_disabled_rejects(self):
        runtime = make_runtime({"groups": {"300": {"agent": {}}}})
        client = FakeClient(roles={888: "admin", 101: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime), "delete_msg", message_id=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "moderation_disabled")
        self.assertEqual(client.calls, [])

    async def test_bot_not_admin_rejects(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "member", 101: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime), "delete_msg", message_id=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bot_not_group_admin")

    async def test_bot_role_api_failure_rejects(self):
        class BrokenClient(FakeClient):
            async def get_group_member_info(self, group_id, user_id):
                raise RuntimeError("api down")
        runtime = make_runtime(MODERATION_CONFIG)
        dispatcher = make_dispatcher(runtime, BrokenClient())
        result = await self.execute(
            runtime, dispatcher, group_event(runtime), "delete_msg", message_id=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bot_not_group_admin")

    async def test_protected_super_owner_target_rejects(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime),
            "set_group_ban", user_id=999, duration=60)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "moderation_target_protected")

    async def test_protected_group_owner_target_rejects(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner", 202: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime),
            "set_group_ban", user_id=202, duration=60)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "moderation_target_protected")

    async def test_quota_exceeded_rejects(self):
        config = {
            "agent": {"moderation_daily_limit": 1},
            "groups": {"300": {"agent": {"moderation_enabled": True}}},
        }
        runtime = make_runtime(config)
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        event = group_event(runtime)
        first = await self.execute(
            runtime, dispatcher, event, "set_group_ban", user_id=201, duration=60)
        second = await self.execute(
            runtime, dispatcher, event, "set_group_ban", user_id=201, duration=60)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "moderation_quota_exceeded")
        self.assertEqual(len(client.calls), 1)

    async def test_high_risk_without_confirmation_rejects(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime), "set_group_kick", user_id=201)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "moderation_requires_confirmation")
        self.assertEqual(client.calls, [])

    async def test_high_risk_confirmed_succeeds(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        event = replace(group_event(runtime), metadata={"confirmed": True})
        result = await self.execute(
            runtime, dispatcher, event, "set_group_kick", user_id=201,
            reject_add_request=True, reason="广告")
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [("set_group_kick", 300, 201, True)])
        records = runtime.timeline.list("group:300", kinds={"moderation"})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["metadata"]["action"], "set_group_kick")
        self.assertEqual(records[0]["metadata"]["target"], 201)

    async def test_super_owner_bypasses_high_risk_confirmation(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        event = runtime.build_event({
            "user_id": 999, "message_type": "private", "raw_message": "x"})
        result = await self.execute(
            runtime, dispatcher, event, "set_group_kick", group_id=300, user_id=201)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [("set_group_kick", 300, 201, False)])

    async def test_auto_patrol_forbids_high_risk_even_as_super_owner(self):
        # 巡检事件以主人身份构造，但它是系统事件：踢人必须走人工确认流。
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        event = replace(
            group_event(runtime, user_id=999),
            metadata={"auto_patrol": True})
        result = await self.execute(
            runtime, dispatcher, event, "set_group_kick", user_id=201)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "moderation_patrol_high_risk_forbidden")
        self.assertEqual(client.calls, [])

    async def test_auto_patrol_allows_low_risk(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        event = replace(
            group_event(runtime, user_id=999),
            metadata={"auto_patrol": True})
        result = await self.execute(
            runtime, dispatcher, event, "set_group_ban", user_id=201, duration=120)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [("set_group_ban", 300, 201, 120)])

    async def test_low_risk_delete_msg_succeeds_and_records(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime),
            "delete_msg", message_id=77, reason="广告")
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [("delete_msg", 77)])
        records = runtime.timeline.list("group:300", kinds={"moderation"})
        self.assertEqual(len(records), 1)
        state = runtime.store.read("moderation/group_300.json", {})
        self.assertEqual(state["count"], 1)

    async def test_ban_duration_is_clamped_to_configured_max(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime),
            "set_group_ban", user_id=201, duration=999999)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [("set_group_ban", 300, 201, 600)])

    async def test_non_int_arguments_reject(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        event = group_event(runtime)
        bad_msg = await self.execute(
            runtime, dispatcher, event, "delete_msg", message_id="abc")
        bad_user = await self.execute(
            runtime, dispatcher, event, "set_group_ban", user_id="abc", duration=60)
        bad_bool = await self.execute(
            runtime, dispatcher,
            replace(event, metadata={"confirmed": True}),
            "set_group_add_request", flag="f1", approve="yes")
        for result in (bad_msg, bad_user, bad_bool):
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_tool_argument")
        self.assertEqual(client.calls, [])

    async def test_group_scope_forces_own_group(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = make_dispatcher(runtime, client)
        result = await self.execute(
            runtime, dispatcher, group_event(runtime),
            "set_group_ban", group_id=999, user_id=201, duration=60)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [("set_group_ban", 300, 201, 60)])

    async def test_private_non_super_cannot_moderate(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin"})
        dispatcher = make_dispatcher(runtime, client)
        event = runtime.build_event({
            "user_id": 101, "message_type": "private", "raw_message": "x"})
        result = await self.execute(
            runtime, dispatcher, event, "set_group_ban",
            group_id=300, user_id=201, duration=60)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "moderation_group_scope_required")

    async def test_private_super_needs_explicit_group_id(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin"})
        dispatcher = make_dispatcher(runtime, client)
        event = runtime.build_event({
            "user_id": 999, "message_type": "private", "raw_message": "x"})
        result = await self.execute(
            runtime, dispatcher, event, "delete_msg", message_id=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_id_required_for_owner_private")

    async def test_add_request_requires_confirmation(self):
        # 入群申请处理是高风险动作：验证消息对错无确定性依据，必须人工确认
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin", 101: "owner"})
        dispatcher = make_dispatcher(runtime, client)
        denied = await self.execute(
            runtime, dispatcher, group_event(runtime),
            "set_group_add_request", flag="flag123", approve=False, reason="可疑")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "moderation_requires_confirmation")
        self.assertEqual(client.calls, [])
        confirmed = await self.execute(
            runtime, dispatcher,
            replace(group_event(runtime), metadata={"confirmed": True}),
            "set_group_add_request", flag="flag123", approve=False, reason="可疑")
        self.assertTrue(confirmed["ok"])
        self.assertEqual(
            client.calls, [("set_group_add_request", "flag123", "add", False, "可疑")])

    def test_catalog_gates_moderation_tools(self):
        runtime = make_runtime(MODERATION_CONFIG)
        dispatcher = make_dispatcher(runtime, FakeClient())
        gateway = AgentToolGateway(dispatcher)
        plain = gateway.catalog()
        self.assertNotIn("set_group_kick", plain)
        with_event = gateway.catalog(group_event(runtime))
        self.assertIn("delete_msg", with_event)
        self.assertIn("set_group_kick", with_event)
        self.assertIn("确认", with_event["set_group_kick"])
        self.assertFalse(gateway.is_read_only("set_group_kick"))
        disabled_runtime = make_runtime({"groups": {"300": {"agent": {}}}})
        disabled_gateway = AgentToolGateway(make_dispatcher(disabled_runtime, FakeClient()))
        self.assertNotIn("delete_msg", disabled_gateway.catalog(group_event(disabled_runtime)))


class ModerationConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    def test_plan_with_moderation_tool_requires_confirmation(self):
        self.assertTrue(_plan_requires_confirmation(
            {"tools": [{"name": "delete_msg", "arguments": {}}]}))
        self.assertTrue(_plan_requires_confirmation(
            {"tools": [{"name": "set_group_kick", "arguments": {}}]}))
        self.assertFalse(_plan_requires_confirmation(
            {"tools": [{"name": "get_group_info", "arguments": {}}]}))

    async def test_confirmed_plan_sets_confirmed_flag_and_executes(self):
        config = {
            "bot_owner": 999, "bot_qq": 888,
            "agent": {"group_max_rounds": 1, "group_tool_budget": 5},
            "groups": {"300": {"agent": {"moderation_enabled": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime})()
        gateway = AgentToolGateway(dispatcher)
        runtime.tools = gateway
        runtime.executor = AgentExecutor(gateway, config)
        frozen = {
            "intent": "kick", "reply": "已移出", "needs_confirmation": True,
            "tools": [{"name": "set_group_kick", "arguments": {"user_id": 201}}],
            "task": None,
        }
        result = await runtime.execute_confirmed_plan(
            dispatcher,
            {"user_id": 101, "group_id": 300, "message_type": "group",
             "raw_message": "踢人", "sender": {"role": "owner"}},
            frozen, role="owner")
        self.assertTrue(result["success"])
        self.assertEqual(client.calls, [("set_group_kick", 300, 201, False)])

    async def test_unconfirmed_plan_cannot_execute_high_risk(self):
        config = {
            "bot_owner": 999, "bot_qq": 888,
            "agent": {"group_max_rounds": 1, "group_tool_budget": 5},
            "groups": {"300": {"agent": {"moderation_enabled": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        client = FakeClient(roles={888: "admin", 101: "owner", 201: "member"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime})()
        gateway = AgentToolGateway(dispatcher)
        runtime.tools = gateway
        runtime.executor = AgentExecutor(gateway, config)
        plan = {
            "intent": "kick", "reply": "尝试", "needs_confirmation": False,
            "tools": [{"name": "set_group_kick", "arguments": {"user_id": 201}}],
            "task": None,
        }
        event = runtime.build_event({
            "user_id": 101, "group_id": 300, "message_type": "group",
            "raw_message": "踢人", "sender": {"role": "owner"}})
        await runtime.run_autonomous(dispatcher, event, initial_plan=plan)
        self.assertEqual(client.calls, [])


class ModerationPlanningContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    async def test_group_context_contains_bot_role(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "admin"})
        dispatcher = make_dispatcher(runtime, client)
        runtime.tools = AgentToolGateway(dispatcher)
        event = group_event(runtime)
        context = await runtime._planning_context(dispatcher, event)
        self.assertIn("小汐在本群的身份：管理", context)
        self.assertIn("主动维护秩序", context)

    async def test_member_role_context_stays_passive(self):
        runtime = make_runtime(MODERATION_CONFIG)
        client = FakeClient(roles={888: "member"})
        dispatcher = make_dispatcher(runtime, client)
        runtime.tools = AgentToolGateway(dispatcher)
        context = await runtime._planning_context(dispatcher, group_event(runtime))
        self.assertIn("小汐在本群的身份：成员", context)
        self.assertIn("只观察不处置", context)


class ModerationReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    def make_worker(self, roles, group_agent, runtime=None):
        from bot.agent.worker_service import AgentWorker
        config = {
            "bot_owner": 999, "bot_qq": 888,
            "agent": {
                "proactive_enabled": True, "quiet_start": 0, "quiet_end": 0,
                "topic_cooldown_seconds": 1800, "group_review_interval_seconds": 1800,
            },
            "groups": {"300": {"enabled": True, "agent": group_agent}},
        }
        runtime = runtime or AgentRuntime(config, tempfile.mkdtemp())
        client = FakeClient(roles=roles)
        dispatcher = type("D", (), {
            "config": config, "agent_runtime": runtime, "client": client})()
        return AgentWorker(dispatcher), runtime, dispatcher

    async def test_moderation_review_runs_without_goals(self):
        worker, runtime, _ = self.make_worker(
            {888: "admin"}, {"proactive_enabled": True, "moderation_enabled": True})
        captured = {}

        async def run(dispatcher, event, task_context="", **kwargs):
            captured["task_context"] = task_context
            captured.update(kwargs)
            return {"reply": "已巡检", "needs_confirmation": False, "tools": []}, []

        runtime.run_autonomous = run
        self.assertEqual(await worker._review_group_scope(), "sent")
        self.assertFalse(captured["read_only_tools"])
        self.assertIn("群管巡检", captured["task_context"])

    async def test_non_admin_group_review_stays_read_only(self):
        worker, runtime, _ = self.make_worker(
            {888: "member"}, {"proactive_enabled": True, "moderation_enabled": True})
        runtime.profiles.update("group:300", proactive_topics=["新番"])
        captured = {}

        async def run(dispatcher, event, task_context="", **kwargs):
            captured["task_context"] = task_context
            captured.update(kwargs)
            return {"reply": "复盘", "needs_confirmation": False, "tools": []}, []

        runtime.run_autonomous = run
        self.assertEqual(await worker._review_group_scope(), "sent")
        self.assertTrue(captured["read_only_tools"])
        self.assertIn("复盘", captured["task_context"])

    async def test_moderation_disabled_group_without_goals_skips(self):
        worker, runtime, _ = self.make_worker(
            {888: "admin"}, {"proactive_enabled": True})
        runtime.run_autonomous = AsyncMock(side_effect=AssertionError("should not run"))
        self.assertEqual(await worker._review_group_scope(), "idle")


class NoticeObservationTests(unittest.IsolatedAsyncioTestCase):
    def make_config(self, observation=True):
        return {
            "bot_owner": 999, "bot_qq": 888,
            "agent": {"observation_enabled": observation},
            "groups": {"300": {"enabled": True}},
        }

    def notice_event(self):
        return {
            "post_type": "notice", "notice_type": "group_ban", "group_id": 300,
            "user_id": 201, "operator_id": 101, "sub_type": "ban",
            "duration": 60, "time": 12345,
        }

    async def test_notice_observed_when_enabled(self):
        from bot.events.notice import handle_notice
        runtime = make_runtime(self.make_config())
        dispatcher = type("D", (), {
            "config": runtime.config, "agent_runtime": runtime,
            "client": FakeClient()})()
        await handle_notice(dispatcher, self.notice_event())
        events = runtime.store.read("events/group_300.json", [])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["text"].startswith("[notice]"))
        self.assertIn("禁言", events[0]["text"])

    async def test_notice_observation_skipped_without_runtime(self):
        from bot.events.notice import handle_notice
        dispatcher = type("D", (), {"config": self.make_config()})()
        await handle_notice(dispatcher, self.notice_event())

    async def test_notice_observation_disabled_by_default(self):
        from bot.events.notice import handle_notice
        runtime = make_runtime(self.make_config(observation=False))
        dispatcher = type("D", (), {
            "config": runtime.config, "agent_runtime": runtime,
            "client": FakeClient()})()
        await handle_notice(dispatcher, self.notice_event())
        self.assertEqual(runtime.store.read("events/group_300.json", []), [])

    async def test_group_request_observed(self):
        from bot.events import request as request_module
        runtime = make_runtime(self.make_config())
        dispatcher = type("D", (), {
            "config": runtime.config, "agent_runtime": runtime,
            "client": FakeClient()})()
        event = {
            "post_type": "request", "request_type": "group", "sub_type": "add",
            "group_id": 300, "user_id": 201, "flag": "flag123",
            "comment": "你好", "time": 12345,
        }
        with tempfile.TemporaryDirectory() as root:
            with patch.object(request_module, "_PENDING_PATH",
                              str(Path(root) / "pending.json")), \
                 patch.object(request_module, "is_blacklisted",
                              lambda group_id, user_id: False):
                await request_module.handle_request(dispatcher, event)
        events = runtime.store.read("events/group_300.json", [])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["text"].startswith("[request]"))
        self.assertIn("申请加入本群", events[0]["text"])


class InsightWritebackTests(unittest.TestCase):
    def test_append_custom_dedupes_bounds_and_keeps_manual(self):
        runtime = make_runtime()
        runtime.profiles.update("group:300", customs="人工规则")
        for index in range(15):
            runtime.profiles.append_custom("group:300", "条目{}".format(index))
        customs = runtime.profiles.get("group:300")["customs"]
        lines = [line for line in customs.split("\n") if line.strip()]
        self.assertEqual(len(lines), 10)
        self.assertIn("人工规则", lines)
        runtime.profiles.append_custom("group:300", "条目14")
        self.assertEqual(
            runtime.profiles.get("group:300")["customs"].count("条目14"), 1)

    def test_high_confidence_insights_writeback_once(self):
        from bot.agent.worker_service import AgentWorker
        runtime = make_runtime()
        runtime.profiles.update("group:300", customs="人工规则")
        runtime.insights.add("group:300", "群里常在晚上聊新番", confidence=0.9)
        runtime.insights.add("group:300", "低置信不回写", confidence=0.5)
        worker = AgentWorker(type("D", (), {
            "config": runtime.config, "agent_runtime": runtime})())
        worker._writeback_insights(runtime, "group:300")
        customs = runtime.profiles.get("group:300")["customs"]
        self.assertIn("人工规则", customs)
        self.assertIn("群里常在晚上聊新番", customs)
        self.assertNotIn("低置信不回写", customs)
        worker._writeback_insights(runtime, "group:300")
        self.assertEqual(
            runtime.profiles.get("group:300")["customs"].count("群里常在晚上聊新番"), 1)


class ModerationConfigMigrationTests(unittest.TestCase):
    def test_moderation_defaults_migrate(self):
        from app.config import migrate_config
        config, migrated = migrate_config({})
        agent = config["agent"]
        self.assertTrue(migrated)
        self.assertFalse(agent["moderation_enabled"])
        self.assertEqual(agent["moderation_daily_limit"], 20)
        self.assertEqual(agent["moderation_ban_max_seconds"], 600)
        self.assertEqual(agent["schema_version"], 6)

    def test_existing_moderation_values_survive_migration(self):
        from app.config import migrate_config
        config, _ = migrate_config({"agent": {
            "schema_version": 6, "moderation_enabled": True,
            "moderation_daily_limit": 5, "moderation_ban_max_seconds": 120,
        }})
        agent = config["agent"]
        self.assertTrue(agent["moderation_enabled"])
        self.assertEqual(agent["moderation_daily_limit"], 5)
        self.assertEqual(agent["moderation_ban_max_seconds"], 120)


class ModerationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_toggle_persists_to_dispatcher_config(self):
        from bot.commands import agent as agent_cmd
        from bot.permission import LEVEL_GOWNER

        with tempfile.TemporaryDirectory() as root:
            config_path = str(Path(root) / "config.json")
            config = {"bot_owner": 999, "bot_qq": 888, "groups": {"300": {}}}
            dispatcher = type("Dispatcher", (), {
                "config": config,
                "_config_path": config_path,
                "_reply": AsyncMock(),
            })()
            with patch.object(agent_cmd, "get_user_level",
                              new=AsyncMock(return_value=(LEVEL_GOWNER, ""))):
                await agent_cmd.cmd_agent(dispatcher, 300, 102, "管群 on", "owner", "", [])
            group_agent = dispatcher.config["groups"]["300"]["agent"]
            self.assertTrue(group_agent["moderation_enabled"])
            saved = json.loads(Path(config_path).read_text(encoding="utf-8"))
            self.assertTrue(saved["groups"]["300"]["agent"]["moderation_enabled"])
            dispatcher._reply.assert_awaited_once()

    async def test_moderation_toggle_rejects_plain_member(self):
        from bot.commands import agent as agent_cmd
        from bot.permission import LEVEL_MEMBER

        with tempfile.TemporaryDirectory() as root:
            config = {"bot_owner": 999, "bot_qq": 888, "groups": {"300": {}}}
            dispatcher = type("Dispatcher", (), {
                "config": config,
                "_config_path": str(Path(root) / "config.json"),
                "_reply": AsyncMock(),
            })()
            with patch.object(agent_cmd, "get_user_level",
                              new=AsyncMock(return_value=(LEVEL_MEMBER, ""))):
                await agent_cmd.cmd_agent(dispatcher, 300, 102, "管群 on", "member", "", [])
            self.assertNotIn("agent", dispatcher.config["groups"]["300"])
            reply_text = dispatcher._reply.await_args[0][2]
            self.assertIn("只能由", reply_text)


class AgentBotHelpTests(unittest.IsolatedAsyncioTestCase):
    COMMANDS = {
        "天气": {"help": "查天气 /天气 城市"},
        "master": {"help": "管理群主人 /master add QQ号", "bot_owner_only": True},
    }

    def _dispatcher(self, runtime):
        return type("D", (), {
            "config": runtime.config, "client": FakeClient(),
            "agent_runtime": runtime, "commands": dict(self.COMMANDS)})()

    def test_catalog_advertises_bot_help(self):
        runtime = make_runtime()
        gateway = AgentToolGateway(self._dispatcher(runtime))
        self.assertIn("get_bot_help", gateway.catalog())
        self.assertTrue(gateway.is_read_only("get_bot_help"))

    async def test_execute_filters_by_identity_level(self):
        runtime = make_runtime()
        gateway = AgentToolGateway(self._dispatcher(runtime))
        member_event = runtime.build_event({
            "user_id": 201, "group_id": 300, "message_type": "group",
            "raw_message": "x", "sender": {"role": "member"}})
        denied = await gateway.execute(
            member_event, "get_bot_help", command_or_category="master")
        self.assertFalse(denied["ok"])
        owner_event = runtime.build_event({
            "user_id": 999, "message_type": "private", "raw_message": "x"})
        allowed = await gateway.execute(
            owner_event, "get_bot_help", command_or_category="master")
        self.assertTrue(allowed["ok"])
        self.assertIn("/master", allowed["data"])


if __name__ == "__main__":
    unittest.main()