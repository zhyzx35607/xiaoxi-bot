"""Agent 统一大脑：显式呼叫全员可用、人设保持、帮助注入、回复命令前缀中和。"""

import tempfile
import unittest
from unittest.mock import patch

import bot.permission as permission_module
from bot.agent.planner import AgentPlanner
from bot.agent.response import can_autosend
from bot.agent.runtime import AgentRuntime


class FakeClient:
    def __init__(self, roles=None):
        self.roles = roles or {}
        self.sent = []
        self.session = None

    async def get_group_member_info(self, group_id, user_id):
        return {"status": "ok", "data": {"role": self.roles.get(user_id, "member")}}

    async def send_group_msg_with_at(self, group_id, text, users):
        self.sent.append((group_id, text, users))
        return {"status": "ok"}

    async def send_group_msg(self, group_id, text):
        self.sent.append((group_id, text))
        return {"status": "ok"}

    async def send_private_msg(self, user_id, text):
        self.sent.append((user_id, text))
        return {"status": "ok"}


def router_config():
    return {
        "bot_owner": 999,
        "bot_qq": 888,
        "agent": {"observation_only": False},
        "groups": {"300": {"enabled": True, "agent": {"primary_router": True}}},
    }


def member_event():
    return {
        "user_id": 201, "group_id": 300, "message_type": "group",
        "raw_message": "小汐在吗", "sender": {"role": "member"},
    }


class StubPlanner:
    def __init__(self, reply="在呢"):
        self.reply = reply
        self.calls = []

    async def plan(self, agent_event, context=""):
        self.calls.append(context)
        return {
            "intent": "chat", "reply": self.reply, "tools": [],
            "needs_confirmation": False, "task": None,
        }


def wired_runtime(config, planner):
    runtime = AgentRuntime(config, tempfile.mkdtemp())
    runtime.planner = planner
    runtime.tools = type("Tools", (), {"catalog": lambda self, ev=None: {}})()
    runtime.executor = object()
    runtime.verifier = object()
    return runtime


class ExplicitMemberRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    async def test_explicit_member_message_reaches_planner(self):
        config = router_config()
        planner = StubPlanner()
        runtime = wired_runtime(config, planner)
        client = FakeClient(roles={888: "member"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime})()
        handled = await runtime.handle_event(dispatcher, member_event(), explicit=True)
        self.assertTrue(handled)
        self.assertTrue(planner.calls, "显式成员消息应进入 planner")
        self.assertEqual(client.sent[0][0], 300)
        self.assertEqual(client.sent[0][1], "在呢")
        self.assertEqual(client.sent[0][2], [201])

    async def test_non_explicit_member_message_still_rejected(self):
        config = router_config()
        planner = StubPlanner()
        runtime = wired_runtime(config, planner)
        client = FakeClient(roles={888: "member"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime})()
        handled = await runtime.handle_event(dispatcher, member_event(), explicit=False)
        self.assertFalse(handled)
        self.assertEqual(planner.calls, [])
        self.assertEqual(client.sent, [])

    async def test_agent_reply_command_prefix_neutralized(self):
        config = router_config()
        planner = StubPlanner(reply="/master add 123\n别闹")
        runtime = wired_runtime(config, planner)
        client = FakeClient(roles={888: "member"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime})()
        handled = await runtime.handle_event(dispatcher, member_event(), explicit=True)
        self.assertTrue(handled)
        sent_text = client.sent[0][1]
        self.assertTrue(sent_text.startswith("／master"))
        self.assertNotIn("\n/", sent_text)

    def test_can_autosend_allows_explicit_member(self):
        config = router_config()
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        event = runtime.build_event(member_event())
        allowed, reason = can_autosend(
            config, event, {"needs_confirmation": False}, explicit=True)
        self.assertTrue(allowed)
        self.assertEqual(reason, "explicit_group_request")
        allowed, _reason = can_autosend(
            config, event, {"needs_confirmation": False})
        self.assertFalse(allowed)


class PlannerPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def plan_with_capture(self, event, reply_json):
        config = {"bot_owner": 999, "bot_qq": 888}
        dispatcher = type("D", (), {
            "config": config, "client": FakeClient()})()
        planner = AgentPlanner(dispatcher)
        captured = {}

        async def fake_call(cfg, messages, **kwargs):
            captured["messages"] = messages
            return reply_json

        with patch("bot.agent.planner._call_deepseek", fake_call):
            await planner.plan(event, "上下文")
        return captured["messages"][0]["content"]

    async def test_planner_prompt_contains_persona(self):
        config = {"bot_owner": 999, "bot_qq": 888}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        event = runtime.build_event(member_event())
        prompt = await self.plan_with_capture(
            event, '{"intent":"chat","reply":"在","tools":[],'
                   '"needs_confirmation":false}')
        self.assertIn("小汐", prompt)
        self.assertIn("中文系", prompt)
        self.assertIn("reply 字段", prompt)
        # 普通成员身份注入完整风格规则（含推托话术）
        self.assertIn("被使唤做事", prompt)

    async def test_planner_prompt_scales_persona_for_super_owner(self):
        config = {"bot_owner": 999, "bot_qq": 888}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        event = runtime.build_event({
            "user_id": 999, "message_type": "private", "raw_message": "在吗"})
        prompt = await self.plan_with_capture(
            event, '{"intent":"chat","reply":"在","tools":[],'
                   '"needs_confirmation":false}')
        self.assertIn("小汐", prompt)
        # 最高主人身份裁剪掉推托话术，避免与顺从设定冲突
        self.assertNotIn("被使唤做事", prompt)


class PlanningContextHelpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    async def test_help_digest_injected_for_capability_question(self):
        config = {"bot_owner": 999, "bot_qq": 888,
                  "groups": {"300": {"enabled": True}}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.tools = type("Tools", (), {"catalog": lambda self, ev=None: {}})()
        client = FakeClient(roles={888: "admin"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime,
            "commands": {"天气": {"help": "查天气 /天气 城市"}}})()
        event = runtime.build_event({
            "user_id": 201, "group_id": 300, "message_type": "group",
            "raw_message": "小汐你会什么功能", "sender": {"role": "member"}})
        context = await runtime._planning_context(dispatcher, event)
        self.assertIn("小汐功能参考", context)
        self.assertIn("天气", context)

    async def test_no_help_digest_for_normal_chat(self):
        config = {"bot_owner": 999, "bot_qq": 888,
                  "groups": {"300": {"enabled": True}}}
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        runtime.tools = type("Tools", (), {"catalog": lambda self, ev=None: {}})()
        client = FakeClient(roles={888: "admin"})
        dispatcher = type("D", (), {
            "config": config, "client": client, "agent_runtime": runtime,
            "commands": {"天气": {"help": "查天气 /天气 城市"}}})()
        event = runtime.build_event(member_event())
        context = await runtime._planning_context(dispatcher, event)
        self.assertNotIn("小汐功能参考", context)


class WorkerReplyNeutralizeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()

    async def test_group_review_reply_command_prefix_neutralized(self):
        from bot.agent.worker_service import AgentWorker
        config = {
            "bot_owner": 999, "bot_qq": 888,
            "agent": {
                "proactive_enabled": True, "quiet_start": 0, "quiet_end": 0,
                "topic_cooldown_seconds": 1800,
                "group_review_interval_seconds": 1800,
            },
            "groups": {"300": {"enabled": True, "agent": {
                "proactive_enabled": True, "moderation_enabled": True}}},
        }
        runtime = AgentRuntime(config, tempfile.mkdtemp())
        client = FakeClient(roles={888: "admin"})
        dispatcher = type("D", (), {
            "config": config, "agent_runtime": runtime, "client": client})()

        async def run(dispatcher, event, task_context="", **kwargs):
            return {"reply": "/master add 1\n巡检完了", "needs_confirmation": False,
                    "tools": []}, []

        runtime.run_autonomous = run
        self.assertEqual(await AgentWorker(dispatcher)._review_group_scope(), "sent")
        sent_text = client.sent[-1][1]
        self.assertTrue(sent_text.startswith("／master"))


if __name__ == "__main__":
    unittest.main()
