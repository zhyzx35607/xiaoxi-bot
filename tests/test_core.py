import unittest
import asyncio
import os
import tempfile
from datetime import datetime
from unittest.mock import patch

from api_registry import REGISTRY
from bot.ai import (
    _await_with_private_typing,
    _call_deepseek_inner,
    _notify_ai_unavailable,
    _should_consider_napcat_tool,
    format_ai_provider_status,
    get_ai_provider_status,
)
from bot.client import OneBotClient
from bot.dispatcher import Dispatcher, _log_chat_message, _read_tail_text
from bot import scheduler


class CoreBehaviorTests(unittest.TestCase):
    def test_chat_log_excludes_disabled_groups_but_keeps_private(self):
        class DispatcherStub:
            config = {
                "groups": {
                    "10001": {"enabled": True},
                    "10002": {"enabled": False},
                }
            }

        dispatcher = DispatcherStub()
        with patch("bot.dispatcher.chat_log.info") as info:
            self.assertTrue(_log_chat_message(
                dispatcher, "GROUP_IN", "enabled", group_id=10001, user_id=1))
            self.assertFalse(_log_chat_message(
                dispatcher, "GROUP_IN", "disabled", group_id=10002, user_id=2))
            self.assertFalse(_log_chat_message(
                dispatcher, "GROUP_IN", "unknown", group_id=10003, user_id=3))
            self.assertTrue(_log_chat_message(
                dispatcher, "PRIVATE_IN", "private", user_id=4))
        self.assertEqual(info.call_count, 2)

    def test_tail_reader_is_bounded_to_requested_lines(self):
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for number in range(20):
                    handle.write(f"line-{number}\n")
            self.assertEqual(
                _read_tail_text(path, line_count=3),
                "line-17\nline-18\nline-19",
            )
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_api_result_normalization(self):
        result = OneBotClient._normalize_result(
            "get_group_info", {"status": "ok", "retcode": 0, "data": {"group_id": 1}}
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error_kind"])

    def test_mutating_apis_are_not_ai_allowed(self):
        for name in (
            "send_group_msg", "delete_msg", "upload_group_file",
            "delete_group_folder", "set_qq_avatar", "create_collection",
        ):
            with self.subTest(name=name):
                self.assertFalse(REGISTRY[name].ai_allowed)
                self.assertNotEqual(REGISTRY[name].risk, "read")

    def test_napcat_tool_gate(self):
        self.assertFalse(_should_consider_napcat_tool("今天晚饭吃什么"))
        self.assertTrue(_should_consider_napcat_tool("看看群公告写了什么"))
        self.assertTrue(_should_consider_napcat_tool("刚才谁是群主"))

    def test_month_end_midnight_calculation(self):
        original = scheduler.datetime

        class MonthEndDateTime:
            @staticmethod
            def now():
                return datetime(2026, 7, 31, 23, 59, 30)

        scheduler.datetime = MonthEndDateTime
        try:
            self.assertEqual(scheduler._seconds_until_next_midnight(), 31)
        finally:
            scheduler.datetime = original


class AsyncCoreBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_typing_is_cleared_when_ai_fails(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def call(self, action, params):
                self.calls.append((action, params))
                return {"status": "ok", "retcode": 0}

        class DispatcherStub:
            client = Client()

        async def fail():
            raise RuntimeError("provider failed")

        dispatcher = DispatcherStub()
        with self.assertRaises(RuntimeError):
            await _await_with_private_typing(dispatcher, 12345, fail())
        self.assertEqual(dispatcher.client.calls, [
            ("set_input_status", {"user_id": 12345, "event_type": 1}),
            ("set_input_status", {"user_id": 12345, "event_type": 0}),
        ])

    async def test_ai_outage_notice_only_for_direct_conversations(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def send_private_msg(self, user_id, text):
                self.calls.append(("private", user_id, text))
                return {"status": "ok"}

            async def send_group_msg_with_at(self, group_id, text, users):
                self.calls.append(("group", group_id, text, users))
                return {"status": "ok"}

        class DispatcherStub:
            client = Client()

        dispatcher = DispatcherStub()
        self.assertFalse(await _notify_ai_unavailable(dispatcher, 10001, 20002, explicit=False))
        self.assertTrue(await _notify_ai_unavailable(dispatcher, 10001, 20002, explicit=True))
        self.assertTrue(await _notify_ai_unavailable(dispatcher, None, 20002))
        self.assertEqual([call[0] for call in dispatcher.client.calls], ["group", "private"])

    async def test_friend_refresh_is_shared_between_concurrent_messages(self):
        class Client:
            def __init__(self):
                self.calls = 0

            async def call(self, action, params):
                self.calls += 1
                await asyncio.sleep(0.01)
                return {
                    "status": "ok",
                    "data": [{"user_id": 101}, {"user_id": 102}],
                }

        client = Client()
        dispatcher = Dispatcher({"runtime": {}}, client)
        results = await asyncio.gather(
            dispatcher._is_friend(101), dispatcher._is_friend(102))
        self.assertEqual(results, [True, True])
        self.assertEqual(client.calls, 1)

    async def test_friend_refresh_failure_is_fail_closed_and_throttled(self):
        class Client:
            def __init__(self):
                self.calls = 0

            async def call(self, action, params):
                self.calls += 1
                return {"status": "failed"}

        client = Client()
        dispatcher = Dispatcher({"runtime": {}}, client)
        self.assertFalse(await dispatcher._is_friend(999))
        self.assertFalse(await dispatcher._is_friend(999))
        self.assertEqual(client.calls, 1)

    async def test_automatic_reaction_uses_numeric_emoji_id(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def set_msg_emoji_like(self, message_id, emoji_id):
                self.calls.append((message_id, emoji_id))
                return {"status": "ok"}

        client = Client()
        dispatcher = Dispatcher({"runtime": {}}, client)
        await dispatcher._send_emoji_reaction(10001, 42, "笑死了")
        self.assertEqual(client.calls, [(42, "128514")])

    async def test_deepseek_request_uses_bounded_timeout(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class Session:
            def __init__(self):
                self.timeout = None

            def post(self, _url, **kwargs):
                self.timeout = kwargs["timeout"].total
                return Response()

        session = Session()
        config = {
            "deepseek_api_key": "test-key",
            "runtime": {"deepseek_timeout_seconds": 99},
        }
        with patch.dict("os.environ", {"AGNES_API_KEY": "", "QQBOT_AGNES_API_KEY": ""}):
            reply = await _call_deepseek_inner(config, [{"role": "user", "content": "hi"}],
                                               session=session)
        self.assertEqual(reply, "ok")
        self.assertEqual(session.timeout, 30)
        providers = {item["name"]: item for item in get_ai_provider_status(config)}
        self.assertGreaterEqual(providers["DeepSeek"].get("successes", 0), 1)
        self.assertIn("DeepSeek", format_ai_provider_status(config))

    async def test_manual_checkin_rejects_disabled_group_and_records_success(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def send_group_sign(self, group_id):
                self.calls.append(group_id)
                return {"status": "ok", "retcode": 0}

        class DispatcherStub:
            config = {
                "groups": {
                    "10001": {"enabled": True},
                    "10002": {"enabled": False},
                }
            }
            client = Client()

        dispatcher = DispatcherStub()
        fd, path = tempfile.mkstemp()
        os.close(fd)
        os.remove(path)
        try:
            with patch.object(scheduler, "_CHECKIN_STATUS_PATH", path):
                ok, text = await scheduler.run_manual_checkin(dispatcher, "10002")
                self.assertFalse(ok)
                self.assertIn("未启用", text)
                ok, text = await scheduler.run_manual_checkin(dispatcher, "10001")
                self.assertTrue(ok)
                self.assertIn("调用成功", text)
                status_text = scheduler.format_checkin_status(dispatcher)
                self.assertIn("10001：成功", status_text)
            self.assertEqual(dispatcher.client.calls, [10001])
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_daily_checkin_only_enabled_groups_uses_native_checkin(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def send_group_sign(self, group_id):
                self.calls.append(("send_group_sign", group_id))
                return {"status": "ok", "retcode": 0}

        class Dispatcher:
            config = {
                "groups": {
                    "10001": {"enabled": True},
                    "10002": {"enabled": False},
                },
                "runtime": {},
            }
            client = Client()

        dispatcher = Dispatcher()
        fd, path = tempfile.mkstemp()
        os.close(fd)
        os.remove(path)
        try:
            with patch.object(scheduler, "_CHECKIN_STATUS_PATH", path):
                await scheduler._daily_checkin(dispatcher)
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertEqual(dispatcher.client.calls, [
            ("send_group_sign", 10001),
        ])


if __name__ == "__main__":
    unittest.main()
