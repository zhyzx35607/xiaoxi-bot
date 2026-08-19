"""Regression tests for the capability and identity overhaul."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot.dispatcher import Dispatcher
from bot.transport.output import send_text_response


class _OutputClient:
    def __init__(self, forward_id=7788):
        self.forward_id = forward_id
        self.group_messages = []
        self.forwards = []
        self.session = None

    async def send_group_msg(self, group_id, message):
        self.group_messages.append((group_id, message))
        return {"status": "ok", "data": {"message_id": 1}}

    async def send_private_msg(self, user_id, message):
        return {"status": "ok", "data": {"message_id": 1}}

    async def send_group_forward_msg(self, group_id, nodes):
        self.forwards.append((group_id, nodes))
        data = {"message_id": self.forward_id} if self.forward_id else {}
        return {"status": "ok", "data": data}

    async def send_private_forward_msg(self, user_id, nodes):
        return {"status": "ok", "data": {"message_id": self.forward_id}}


class LongOutputTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher(self, forward_id=7788):
        client = _OutputClient(forward_id)
        config = {
            "bot_owner": 1, "bot_qq": 2,
            "message_output": {"forward_threshold_chars": 200,
                               "ai_summary_enabled": False,
                               "reply_to_forward": True},
        }
        return type("Stub", (), {"config": config, "client": client})()

    async def test_199_200_201_character_boundary(self):
        dispatcher = self._dispatcher()
        await send_text_response(dispatcher, 100, 1, "a" * 199)
        await send_text_response(dispatcher, 100, 1, "a" * 200)
        await send_text_response(dispatcher, 100, 1, "a" * 201)
        self.assertEqual(len(dispatcher.client.forwards), 1)
        self.assertEqual(len(dispatcher.client.group_messages), 3)
        guide = dispatcher.client.group_messages[-1][1]
        self.assertEqual(guide[0]["type"], "reply")
        self.assertEqual(guide[0]["data"]["id"], "7788")
        self.assertEqual(guide[1]["type"], "at")

    async def test_missing_forward_id_replies_to_original_command(self):
        dispatcher = self._dispatcher(forward_id=0)
        await send_text_response(
            dispatcher, 100, 1, "a" * 201, request_message_id=5566)
        guide = dispatcher.client.group_messages[-1][1]
        self.assertEqual(guide[0]["data"]["id"], "5566")

    async def test_help_summary_never_calls_ai(self):
        dispatcher = self._dispatcher()
        with patch("bot.ai.providers._call_deepseek", new=AsyncMock()) as model:
            await send_text_response(
                dispatcher, 100, 1, "help", force_forward=True, kind="help")
        model.assert_not_awaited()

    async def test_oversized_forward_is_chunked(self):
        # QQ rejects merged forwards above ~100 nodes; long help output must split.
        dispatcher = self._dispatcher()
        sections = ["第{}条命令说明".format(i) for i in range(120)]
        await send_text_response(
            dispatcher, 100, 1, "ignored", force_forward=True,
            kind="help", title="小汐的完整帮助", sections=sections)
        self.assertEqual(len(dispatcher.client.forwards), 3)
        for _group, nodes in dispatcher.client.forwards:
            self.assertLessEqual(len(nodes), 50)
        titles = [nodes[0]["data"]["content"] for _group, nodes in dispatcher.client.forwards]
        self.assertIn("(1/3)", titles[0])
        self.assertIn("(3/3)", titles[2])

    async def test_failed_forward_falls_back_to_notice(self):
        class FailingClient(_OutputClient):
            async def send_group_forward_msg(self, group_id, nodes):
                return {"status": "failed", "retcode": 1200}

            async def send_forward_msg(self, **kwargs):
                return {"status": "failed", "retcode": 1200}

            async def send_group_msg(self, group_id, message):
                # Plain-message degradation must also fail before the notice.
                self.group_messages.append((group_id, message))
                return {"status": "failed", "retcode": 1200}

        dispatcher = self._dispatcher()
        dispatcher.client = FailingClient()
        await send_text_response(
            dispatcher, 100, 1, "a" * 500, force_forward=True, kind="help")
        last = dispatcher.client.group_messages[-1][1]
        self.assertIn("等会再试", str(last))


class GroupHelpCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_random_image_help_uses_full_filter_reference(self):
        from bot.commands.random_image import RANDOM_IMAGE_HELP
        from bot.commands.system import cmd_help
        from bot.permission import LEVEL_SUPER

        replies = []
        dispatcher = type("Dispatcher", (), {})()
        dispatcher.commands = {
            "随机图": {"help": "随机图片 /随机图 [标签与范围]"},
        }
        dispatcher.config = {"bot_owner": 100, "bot_qq": 200}

        async def reply(*args, **kwargs):
            replies.append((args, kwargs))

        dispatcher._reply = reply
        with patch("bot.commands.system.get_user_level", new=AsyncMock(
                return_value=(LEVEL_SUPER, "super"))), patch(
                "bot.commands.system.get_bot_role", new=AsyncMock(
                    return_value=("owner", "owner"))):
            await cmd_help(dispatcher, 100, 100, "随机图", "owner", "主人", [])

        self.assertEqual(len(replies), 1)
        self.assertIn(RANDOM_IMAGE_HELP, replies[0][0][2])

    async def test_undocumented_command_gets_explicit_format_fallback(self):
        from bot.commands.system import _help_command_text

        text = _help_command_text(
            "群信息", {"help": "查看群信息"}, "owner", 100)
        self.assertIn("格式：/群信息 [参数]", text)
        self.assertIn("格式示例：/群信息 [参数]", text)

    async def test_group_help_renders_for_super_owner(self):
        from bot.commands.system import cmd_help
        from bot.permission import LEVEL_SUPER

        replies = []
        dispatcher = type("Dispatcher", (), {})()
        dispatcher.commands = {
            "help": {"help": "查看可用命令"},
        }
        dispatcher.config = {"bot_owner": 100, "bot_qq": 200}

        async def reply(*args, **kwargs):
            replies.append((args, kwargs))

        dispatcher._reply = reply
        with patch("bot.commands.system.get_user_level", new=AsyncMock(return_value=(LEVEL_SUPER, "super"))),              patch("bot.commands.system.get_bot_role", new=AsyncMock(return_value=("owner", "owner"))):
            await cmd_help(dispatcher, 100, 100, "", "owner", "主人", [])

        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][1]["force_forward"])
        self.assertIn("help", replies[0][0][2])

    async def test_full_help_is_compact_and_drill_down_hinted(self):
        # 完整菜单必须紧凑：逐命令详细格式全量超一万字，转发和普通消息都发不出
        from bot.commands.system import COMMAND_DETAILS, cmd_help
        from bot.permission import LEVEL_SUPER

        replies = []
        dispatcher = type("Dispatcher", (), {})()
        dispatcher.commands = {
            "help": {"help": "查看可用命令"},
            "master": {"help": "管理群主人", "bot_owner_only": True},
        }
        dispatcher.config = {"bot_owner": 100, "bot_qq": 200}

        async def reply(*args, **kwargs):
            replies.append((args, kwargs))

        dispatcher._reply = reply
        with patch("bot.commands.system.get_user_level", new=AsyncMock(return_value=(LEVEL_SUPER, "super"))), \
                patch("bot.commands.system.get_bot_role", new=AsyncMock(return_value=("owner", "owner"))):
            await cmd_help(dispatcher, 100, 100, "", "owner", "主人", [])

        text = replies[0][0][2]
        # 紧凑菜单不含逐命令详情，且指引 drill-down
        self.assertNotIn(COMMAND_DETAILS["master"], text)
        self.assertIn("/help 命令名", text)
        self.assertIn("/master", text)


class OwnerReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_name_call_bypasses_disabled_group_and_rate_limits(self):
        client = _OutputClient()
        client.get_group_member_info = AsyncMock(return_value={
            "status": "ok", "data": {"role": "member"}})
        config = {
            "bot_owner": 1, "bot_qq": 2, "command_prefix": "/",
            "groups": {"100": {"enabled": False, "features": {"ai_chat": False}}},
            "group_defaults": {"features": {}},
            "name_mention": {"enabled": True, "names": ["xiaoxi"],
                             "cooldown_seconds": 999, "user_cooldown_seconds": 999},
            "bilibili": {"parse_enabled": False},
            "runtime": {}, "chat_limits": {}, "sticker_mode": {"enabled": False},
        }
        dispatcher = Dispatcher(config, client)
        dispatcher._check_global_rate_limit = lambda: False
        dispatcher._check_rate_limit = lambda group_id: (False, 0)
        dispatcher._do_ai_reply = AsyncMock(return_value=True)
        event = {
            "post_type": "message", "message_type": "group", "group_id": 100,
            "user_id": 1, "message_id": 99, "raw_message": "xiaoxi call me master",
            "message": [{"type": "text", "data": {"text": "xiaoxi call me master"}}],
            "sender": {"role": "member", "nickname": "owner"},
        }
        with patch("bot.events.message.is_blacklisted", return_value=False), \
             patch("bot.notice_handler.check_bad_words", new=AsyncMock(return_value=False)):
            await dispatcher._handle_message(event)
        dispatcher._do_ai_reply.assert_awaited_once()


class ToolScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_friend_tools_are_owner_only(self):
        import ai_tools
        client = type("Client", (), {
            "get_group_member_info": AsyncMock(return_value={"status": "ok", "data": {"role": "member"}}),
            "get_friend_list": AsyncMock(return_value={"status": "ok", "data": []}),
        })()
        dispatcher = type("Stub", (), {
            "config": {"bot_owner": 1, "bot_qq": 2, "ai_tools": {}},
            "client": client,
        })()
        denied = await ai_tools.execute_ai_tool(
            dispatcher, "get_friend_list", {}, user_id=9, interaction_allowed=True)
        allowed = await ai_tools.execute_ai_tool(
            dispatcher, "get_friend_list", {}, user_id=1, interaction_allowed=True)
        self.assertEqual(denied["error"], "permission_denied")
        self.assertTrue(allowed["ok"])

    async def test_group_tool_cannot_override_current_group(self):
        import ai_tools
        calls = []
        client = type("Client", (), {})()
        async def group_info(group_id):
            calls.append(group_id)
            return {"status": "ok", "data": {}}
        client.get_group_info = group_info
        client.get_group_member_info = AsyncMock(return_value={"status": "ok", "data": {"role": "member"}})
        dispatcher = type("Stub", (), {
            "config": {"bot_owner": 1, "bot_qq": 2, "groups": {"100": {}},
                       "group_defaults": {}, "ai_tools": {}},
            "client": client,
        })()
        result = await ai_tools.execute_ai_tool(
            dispatcher, "get_group_info", {"group_id": 999},
            group_id=100, user_id=1, interaction_allowed=True)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [100])


class UApiEnhancementTests(unittest.TestCase):
    def test_retry_after_and_rate_limit_headers(self):
        from bot.integrations import uapi
        self.assertEqual(uapi._retry_after_seconds({"Retry-After": "3"}, 0), 3)
        uapi.reset_state_for_test()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "uapi-state.json")
            with patch.object(uapi, "_STATE_PATH", state_path):
                uapi._record_response({}, "/saying", "user", 200, {
                    "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "88",
                    "X-RateLimit-Reset": "123", "Uapi-Credits-Charged": "0",
                })
                status = uapi.credits_remaining({})
        self.assertEqual(status["rate_limit"], 100)
        self.assertEqual(status["rate_remaining"], 88)

    def test_image_resolver_cools_down_after_rate_limit(self):
        from bot.integrations import uapi

        class Response:
            status = 429
            headers = {"Retry-After": "120"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                return Response()

        class Client:
            session = Session()

        class Stub:
            config = {}
            client = Client()

        with tempfile.TemporaryDirectory() as directory, patch.object(
            uapi, "_STATE_PATH", os.path.join(directory, "uapi.json")
        ):
            uapi.reset_state_for_test()
            dispatcher = Stub()
            first = asyncio.run(uapi.uapi_resolve_image_url(dispatcher, "/random/image"))
            second = asyncio.run(uapi.uapi_resolve_image_url(dispatcher, "/random/image"))

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(Stub.client.session.calls, 1)

    def test_binary_requests_honor_shared_cooldown(self):
        from bot.integrations import uapi

        class Response:
            status = 429
            headers = {"Retry-After": "120"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                return Response()

        dispatcher = type("Dispatcher", (), {
            "config": {}, "client": type("Client", (), {"session": Session()})(),
        })()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                uapi, "_STATE_PATH", os.path.join(directory, "uapi.json")):
            uapi.reset_state_for_test()
            first = asyncio.run(uapi.uapi_get_binary(dispatcher, "/image/qrcode"))
            second = asyncio.run(uapi.uapi_get_binary(dispatcher, "/image/qrcode"))
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(dispatcher.client.session.calls, 1)

    def test_rate_limit_streak_is_persisted_and_backoff_grows(self):
        from bot.integrations import uapi

        dispatcher = type("Dispatcher", (), {})()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            uapi, "_STATE_PATH", os.path.join(directory, "uapi.json")
        ), patch.object(uapi.time, "time", return_value=1000):
            uapi.reset_state_for_test()
            first = uapi._start_rate_limit_cooldown(dispatcher, "/random/image", {})
            uapi._load_state()["rate_limit_endpoints"]["/random/image"]["until"] = 1000
            second = uapi._start_rate_limit_cooldown(dispatcher, "/random/image", {})
            uapi.reset_state_for_test()
            restored = uapi._load_state()["rate_limit_endpoints"]["/random/image"]
        self.assertEqual(first, 60)
        self.assertEqual(second, 120)
        self.assertEqual(restored["streak"], 2)

    def test_json_requests_enter_shared_cooldown_after_rate_limit(self):
        from bot.integrations import uapi

        class Response:
            status = 429
            headers = {"Retry-After": "120"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self):
                self.calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                return Response()

        class Client:
            def __init__(self):
                self.session = Session()

        class Stub:
            config = {}

            def __init__(self):
                self.client = Client()

        dispatcher = Stub()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            uapi, "_STATE_PATH", os.path.join(directory, "uapi.json")
        ):
            uapi.reset_state_for_test()
            first = asyncio.run(uapi.uapi_get(dispatcher, "/misc/hotboard"))
            second = asyncio.run(uapi.uapi_get(dispatcher, "/misc/hotboard"))

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(dispatcher.client.session.calls, 1)

    def test_json_response_size_is_bounded(self):
        from bot.integrations import uapi

        class Response:
            status = 200
            headers = {"Content-Length": str(2 * 1024 * 1024)}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, **kwargs):
                raise AssertionError("oversized body must be rejected before parsing")

        class Session:
            def request(self, *args, **kwargs):
                return Response()

        dispatcher = type("Dispatcher", (), {
            "config": {}, "client": type("Client", (), {"session": Session()})(),
        })()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                uapi, "_STATE_PATH", os.path.join(directory, "uapi.json")):
            uapi.reset_state_for_test()
            self.assertIsNone(asyncio.run(uapi.uapi_get(dispatcher, "/misc/hotboard")))


class ApiRegistryMetadataTests(unittest.TestCase):
    def test_each_action_has_exactly_one_category(self):
        from api_registry import _NAMES

        seen = {}
        for category, names in _NAMES.items():
            for name in names:
                self.assertNotIn(name, seen, "duplicate category for %s" % name)
                seen[name] = category

    def test_set_group_add_request_is_request_category_management_risk(self):
        from api_registry import REGISTRY

        spec = REGISTRY["set_group_add_request"]
        self.assertEqual(spec.category, "request")
        self.assertEqual(spec.risk, "management")
        self.assertFalse(spec.ai_allowed)
        self.assertFalse(spec.automation_allowed)
