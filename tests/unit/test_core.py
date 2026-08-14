"""Core unit and behavioral regression tests."""

import unittest
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
from bot.integrations.mukyu import MukyuImage
from bot import scheduler


class CoreBehaviorTests(unittest.TestCase):
    def test_ai_memory_redacts_common_credential_formats(self):
        from bot.ai import memory as ai_memory

        entries = [{
            "role": "user",
            "content": (
                "Cookie: session=private-cookie; "
                "Authorization: Bearer private-bearer; "
                "github_pat_abcdefghijklmnopqrstuvwxyz123456"
            ),
        }]
        with tempfile.TemporaryDirectory() as root, \
                patch.object(ai_memory, "MEMORY_DIR", root):
            ai_memory._memories.pop(987654, None)
            ai_memory._memory_timestamps.pop(987654, None)
            ai_memory._save_memory(987654, entries)
            saved = Path(root, "group_987654.json").read_text(encoding="utf-8")

        self.assertNotIn("private-cookie", saved)
        self.assertNotIn("private-bearer", saved)
        self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz123456", saved)
        self.assertIn("[已隐藏]", saved)

        from bot.memory import redact_sensitive_text
        private_key = "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----"
        self.assertNotIn("private-body", redact_sensitive_text(private_key))
        self.assertNotIn("ASIA1234567890ABCDEF", redact_sensitive_text("ASIA1234567890ABCDEF"))

    def test_ai_memory_redacts_legacy_and_long_term_files_on_read(self):
        from bot.ai import memory as ai_memory

        with tempfile.TemporaryDirectory() as root, \
                patch.object(ai_memory, "MEMORY_DIR", root):
            Path(root, "group_987655.json").write_text(json.dumps([{
                "role": "user", "content": "Cookie: session=legacy-cookie", "ts": time.time(),
            }]), encoding="utf-8")
            Path(root, "group_987655_long.json").write_text(json.dumps([{
                "content": "Authorization: Bearer legacy-bearer", "ts": time.time(),
            }]), encoding="utf-8")
            ai_memory._memories.pop(987655, None)
            ai_memory._memory_timestamps.pop(987655, None)

            short = ai_memory._load_memory(987655)
            long = ai_memory._load_long_memory(987655)
            rewritten_short = Path(root, "group_987655.json").read_text(encoding="utf-8")
            rewritten_long = Path(root, "group_987655_long.json").read_text(encoding="utf-8")

        self.assertNotIn("legacy-cookie", short[0]["content"])
        self.assertNotIn("legacy-bearer", long[0]["content"])
        self.assertIn("[已隐藏]", short[0]["content"])
        self.assertIn("[已隐藏]", long[0]["content"])
        self.assertNotIn("legacy-cookie", rewritten_short)
        self.assertNotIn("legacy-bearer", rewritten_long)

    def test_chat_log_excludes_disabled_groups_and_disabled_private(self):
        class DispatcherStub:
            config = {
                "bot_owner": 9,
                "private_chat": {"enabled": False, "allowed_users": [5]},
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
            self.assertFalse(_log_chat_message(
                dispatcher, "PRIVATE_IN", "private", user_id=4))
            self.assertTrue(_log_chat_message(
                dispatcher, "PRIVATE_IN", "allowed", user_id=5))
            self.assertTrue(_log_chat_message(
                dispatcher, "PRIVATE_IN", "owner", user_id=9))
        self.assertEqual(info.call_count, 3)

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
            "send_group_msg", "send_group_msg_reply", "send_group_msg_with_at",
            "send_flash_msg", "click_inline_keyboard_button", "delete_msg",
            "upload_group_file", "delete_group_folder", "set_qq_avatar",
            "create_collection", "_send_group_notice", "_del_group_notice",
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
        self.assertEqual(client.calls, 3)

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
        with patch.dict("os.environ", {"SIGMAI_API_KEY": "", "QQBOT_SIGMAI_API_KEY": ""}):
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



    def test_split_reply_lines_segments_by_ai_newlines(self):
        from bot.ai import _split_reply_lines
        self.assertEqual(_split_reply_lines("第一句\n第二句"), ["第一句", "第二句"])
        self.assertEqual(_split_reply_lines("只有一句"), ["只有一句"])
        self.assertEqual(_split_reply_lines("a\nb\nc\nd", max_parts=3), ["a", "b", "c d"])

    def test_ai_package_preserves_legacy_import_surface(self):
        import bot.ai as ai_package
        from bot.ai import memory, prompts, providers, reply, runtime, search, stickers, tools

        self.assertIs(ai_package.handle_ai_chat, runtime.handle_ai_chat)
        self.assertIs(ai_package._parse_reply_tags, reply._parse_reply_tags)
        self.assertIs(ai_package._prepare_group_reply, reply._prepare_group_reply)
        self.assertIs(ai_package._build_system_prompt, prompts._build_system_prompt)
        self.assertIs(ai_package.search_web, search.search_web)
        self.assertIs(ai_package._load_memory, memory._load_memory)
        self.assertIs(ai_package._call_deepseek, providers._call_deepseek)
        self.assertIs(ai_package.describe_image, stickers.describe_image)
        self.assertIs(ai_package._chat_with_tools, tools._chat_with_tools)

    def test_transport_package_preserves_legacy_import_surface(self):
        from actions import text_segment as legacy_text_segment
        from bot.client import OneBotClient as legacy_client
        from bot.transport.onebot import OneBotClient
        from bot.transport.segments import text_segment

        self.assertIs(legacy_client, OneBotClient)
        self.assertEqual(legacy_text_segment("hello"), text_segment("hello"))

    def test_events_package_preserves_legacy_import_surface(self):
        from bot.dispatcher import Dispatcher
        from bot.events.message import GroupMessageMixin, PrivateMessageMixin
        from bot.events.notice import handle_notice
        from bot.events.request import handle_request
        from bot.events.router import RouterMixin
        from bot.services.delayed_reply import DelayedReplyServiceMixin
        from bot.services.health import HealthServiceMixin
        from bot.services.member_cache import MemberCacheMixin
        from bot.notice_handler import handle_notice as legacy_notice
        from bot.request_handler import handle_request as legacy_request

        self.assertIs(legacy_notice, handle_notice)
        self.assertIs(legacy_request, handle_request)
        self.assertIs(Dispatcher.dispatch, RouterMixin.dispatch)
        self.assertIs(Dispatcher._handle_group_message, GroupMessageMixin._handle_group_message)
        self.assertIs(Dispatcher._handle_private_ai_chat, PrivateMessageMixin._handle_private_ai_chat)
        self.assertIs(Dispatcher._trigger_delayed_reply, DelayedReplyServiceMixin._trigger_delayed_reply)
        self.assertIs(Dispatcher.start_rss_guard, HealthServiceMixin.start_rss_guard)
        self.assertIs(Dispatcher._refresh_member_cache, MemberCacheMixin._refresh_member_cache)
        self.assertTrue(Dispatcher._read_rss_kb() is None or Dispatcher._read_rss_kb() > 0)
        self.assertIsInstance(Dispatcher._gc_type_histogram(), str)

    def test_commands_package_preserves_registration_and_domain_imports(self):
        import bot.commands as commands_package
        from bot.commands import register_all as legacy_register_all
        from bot.commands.admin import cmd_my_title
        from bot.commands.fun import cmd_fortune
        from bot.commands.media import cmd_ocr
        from bot.commands.moderation import cmd_ban
        from bot.commands.queries import cmd_weather
        from bot.commands.registry import register_all
        from bot.commands.runtime import cmd_my_title as runtime_cmd_my_title
        from bot.commands.system import cmd_health

        self.assertIs(legacy_register_all, register_all)
        self.assertIs(cmd_my_title, runtime_cmd_my_title)
        self.assertIs(commands_package.cmd_my_title, cmd_my_title)
        self.assertIs(commands_package.cmd_fortune, cmd_fortune)
        self.assertIs(commands_package.cmd_ocr, cmd_ocr)
        self.assertIs(commands_package.cmd_ban, cmd_ban)
        self.assertIs(commands_package.cmd_weather, cmd_weather)
        self.assertIs(commands_package.cmd_health, cmd_health)

    def test_infrastructure_boundaries_preserve_legacy_objects(self):
        import main
        from app.bootstrap import amain
        from app.config import load_config
        from bot import bilibili, scheduler, touchgal, uapi
        from bot.integrations import bilibili as integration_bilibili
        from bot.integrations import touchgal as integration_touchgal
        from bot.integrations import uapi as integration_uapi
        from bot.services import scheduler as service_scheduler
        from bot.storage import atomic_write_json
        from bot.utils import atomic_write_json as legacy_atomic_write_json

        self.assertIs(integration_bilibili.poll_once, bilibili.poll_once)
        self.assertIs(integration_touchgal.search_games, touchgal.search_games)
        self.assertIs(integration_uapi.uapi_get, uapi.uapi_get)
        self.assertIs(service_scheduler.scheduler_loop, scheduler.scheduler_loop)
        self.assertIs(atomic_write_json, legacy_atomic_write_json)
        self.assertIs(main.load_config, load_config)
        self.assertIs(main.amain, amain)

    def test_structured_at_uses_real_segment_without_duplicate_text(self):
        from bot.ai import _build_group_reply_segments, _prepare_group_reply

        text, targets, quote, pokes = _prepare_group_reply(
            "[AT:小明]你好", {"小明": 12345}, user_id=67890, message_id=99)
        self.assertEqual(text, "你好")
        self.assertEqual(targets, [12345])
        self.assertIsNone(quote)
        self.assertEqual(pokes, [])
        self.assertEqual(_build_group_reply_segments(text, targets), [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " "}},
            {"type": "text", "data": {"text": "你好"}},
        ])

    def test_unresolved_structured_at_degrades_without_fake_mention(self):
        from bot.ai import _prepare_group_reply

        text, targets, quote, pokes = _prepare_group_reply(
            "[AT:陌生人]你好", {}, user_id=67890, message_id=99)
        self.assertEqual(text, "陌生人你好")
        self.assertEqual(targets, [])
        self.assertNotIn("@", text)
        self.assertIsNone(quote)
        self.assertEqual(pokes, [])

    def test_reply_to_sender_suppresses_duplicate_at(self):
        from bot.ai import _prepare_group_reply

        text, targets, quote, _ = _prepare_group_reply(
            "[REPLY][AT:小明]收到", {"小明": 12345},
            user_id=12345, message_id=99)
        self.assertEqual(text, "收到")
        self.assertEqual(targets, [])
        self.assertEqual(quote, "reply")

    def test_plain_at_requires_nickname_boundary(self):
        from bot.ai import _parse_reply_actions

        text, targets, quote = _parse_reply_actions(
            "@小明 你好", {"小": 1, "小明": 2})
        self.assertEqual(text, "你好")
        self.assertEqual(targets, [2])
        self.assertIsNone(quote)

        text, targets, _ = _parse_reply_actions(
            "@小明你好", {"小": 1, "小明": 2})
        self.assertEqual(text, "小明你好")
        self.assertEqual(targets, [])

    def test_skip_reply_signal_is_recognized(self):
        self.assertTrue("[SKIP]".strip().upper().startswith("[SKIP]"))
        self.assertTrue("[SKIP] 不想接话".strip().upper().startswith("[SKIP]"))

    def test_typing_delay_is_proportional_and_capped(self):
        from bot.ai import _typing_delay_secs
        short = _typing_delay_secs("短")
        long = _typing_delay_secs("啊" * 200)
        self.assertLessEqual(short, 3.0)
        self.assertLessEqual(long, 8.0)
        self.assertGreaterEqual(long, short)

    def test_commit_config_keeps_env_secrets_in_memory_only(self):
        from bot.commands import common
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            dispatcher = type("DispatcherStub", (), {"config": {}})()
            with patch.object(common, "CONFIG_PATH", path), \
                    patch.dict(os.environ, {"QQBOT_DEEPSEEK_API_KEY": "env-secret"}):
                common._commit(dispatcher, {"groups": {}})
            self.assertEqual(dispatcher.config["deepseek_api_key"], "env-secret")
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn("deepseek_api_key", saved)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_save_group_config_strips_secrets(self):
        import tempfile, os, json
        from bot.utils import atomic_write_json
        from bot.permission import save_group_config, get_group_config
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            class DispatcherStub:
                _config_path = path
                config = {
                    "token": "secret-token",
                    "deepseek_api_key": "secret-ds",
                    "sigmai_api_key": "secret-sigmai",
                    "agnes_api_key": "secret-agnes",
                    "vision_api": {"api_key": "secret-vision", "model": "qwen-vl-plus"},
                    "group_defaults": {"bad_words": {"words": ["a"]}},
                    "groups": {"10001": {"enabled": True, "bad_words": {"words": ["b"]}}},
                }
            save_group_config(DispatcherStub())
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn("token", saved)
            self.assertNotIn("deepseek_api_key", saved)
            self.assertNotIn("sigmai_api_key", saved)
            self.assertNotIn("agnes_api_key", saved)
            self.assertNotIn("api_key", saved.get("vision_api", {}))
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_bad_words_are_union_of_default_and_group(self):
        from bot.permission import get_group_config
        class DispatcherStub:
            config = {
                "group_defaults": {"bad_words": {"words": ["a", "b"]}},
                "groups": {"10001": {"enabled": True, "bad_words": {"words": ["b", "c"]}}},
            }
        gcfg = get_group_config(DispatcherStub(), "10001")
        self.assertEqual(gcfg["bad_words"]["words"], ["a", "b", "c"])

    async def test_delayed_queue_merges_and_caps(self):
        client = type("Client", (), {})()
        dispatcher = Dispatcher({"runtime": {}}, client)
        await dispatcher._enqueue_delayed_reply(1, 101, 1001, [], "hello", "A")
        await dispatcher._enqueue_delayed_reply(1, 101, 1002, [], "world", "A")
        self.assertEqual(len(dispatcher._delayed_queue_index), 1)
        self.assertEqual(len(dispatcher._delayed_queue), 2)  # old entry marked stale
        # Cap at 20: should still accept more
        for i in range(30):
            await dispatcher._enqueue_delayed_reply(2, 200 + i, 3000 + i, [], f"msg{i}", f"U{i}")
        self.assertEqual(len(dispatcher._delayed_queue_index), 20)

    async def test_group_ai_reply_saves_state_after_send(self):
        """Regression: context_key must be defined so post-send state updates run."""
        from unittest.mock import AsyncMock
        from bot import ai as ai_module

        sent = []

        class Client:
            session = None

            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                return {"status": "failed"}

            async def send_group_msg(self, group_id, message):
                sent.append(message)
                return {"status": "ok"}

        class DispatcherStub:
            config = {
                "bot_qq": 999999,
                "bot_owner": 888888,
                "runtime": {},
                "sticker_mode": {},
            }
            client = Client()
            _group_member_cache = {}

            def append_to_buffer(self, group_id, user_id, raw_message, card):
                pass

        saved = []
        dispatcher = DispatcherStub()
        with patch.object(ai_module, "_load_memory", return_value=[]), \
             patch.object(ai_module, "_save_memory", lambda *a, **k: saved.append(a)), \
             patch.object(ai_module, "_load_user_memory", return_value=[]), \
             patch.object(ai_module, "_save_user_memory", lambda *a, **k: None), \
             patch.object(ai_module, "_typing_delay_secs", lambda text: 0), \
             patch.object(ai_module.random, "uniform", lambda a, b: 0.0), \
             patch.object(ai_module, "_chat_with_tools",
                          new=AsyncMock(return_value=None)), \
             patch.object(ai_module, "_call_deepseek",
                          new=AsyncMock(return_value="哈哈 确实不错")):
            result = await ai_module.handle_ai_chat(
                dispatcher, 424242, 313131, "今天天气真好啊", "小明",
                web_search_results="",
            )
        self.assertTrue(result)
        self.assertTrue(sent, "reply should have been sent")
        self.assertTrue(saved, "memory should be saved after a successful reply")
        self.assertIn("424242", ai_module._last_reply_ts)

    async def test_group_ai_skip_signal_returns_false_cleanly(self):
        """Regression: [SKIP] path must not raise NameError on context_key."""
        from unittest.mock import AsyncMock
        from bot import ai as ai_module

        sent = []

        class Client:
            session = None

            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                return {"status": "failed"}

            async def send_group_msg(self, group_id, message):
                sent.append(message)
                return {"status": "ok"}

        class DispatcherStub:
            config = {
                "bot_qq": 999999,
                "bot_owner": 888888,
                "runtime": {},
                "sticker_mode": {},
            }
            client = Client()
            _group_member_cache = {}

            def append_to_buffer(self, group_id, user_id, raw_message, card):
                pass

        saved = []
        dispatcher = DispatcherStub()
        with patch.object(ai_module, "_load_memory", return_value=[]), \
             patch.object(ai_module, "_save_memory", lambda *a, **k: saved.append(a)), \
             patch.object(ai_module, "_load_user_memory", return_value=[]), \
             patch.object(ai_module, "_save_user_memory", lambda *a, **k: None), \
             patch.object(ai_module, "_chat_with_tools",
                          new=AsyncMock(return_value=None)), \
             patch.object(ai_module, "_call_deepseek",
                          new=AsyncMock(return_value="[SKIP] 不想接话")):
            result = await ai_module.handle_ai_chat(
                dispatcher, 424243, 313132, "嗯嗯好吧", "小明",
                web_search_results="",
            )
        self.assertFalse(result)
        self.assertFalse(sent, "[SKIP] must not send anything")
        self.assertFalse(saved, "[SKIP] must not write memory")
        self.assertIn("424243", ai_module._last_reply_ts)

    async def test_forward_messages_use_extended_timeout(self):
        from unittest.mock import AsyncMock

        client = OneBotClient({
            "ws_url": "ws://127.0.0.1:3001", "token": "",
            "bot_qq": 222, "runtime": {"api_timeout_seconds": 8},
        })
        with patch.object(client, "call", new=AsyncMock(
                return_value={"status": "ok"})) as call_mock:
            await client.send_group_forward_msg(100, [{"type": "node"}])
        call_mock.assert_awaited_once_with(
            "send_group_forward_msg",
            {"group_id": 100, "messages": [{"type": "node"}]},
            timeout=120,
        )

    async def test_forward_timeout_is_configurable(self):
        from unittest.mock import AsyncMock

        client = OneBotClient({
            "ws_url": "ws://127.0.0.1:3001", "token": "",
            "bot_qq": 222,
            "runtime": {"api_timeout_seconds": 8, "forward_timeout_seconds": 30},
        })
        with patch.object(client, "call", new=AsyncMock(
                return_value={"status": "ok"})) as call_mock:
            await client.send_group_forward_msg(100, [{"type": "node"}])
        call_mock.assert_awaited_once_with(
            "send_group_forward_msg",
            {"group_id": 100, "messages": [{"type": "node"}]},
            timeout=30,
        )

    async def test_media_messages_use_extended_timeout(self):
        client = OneBotClient({
            "ws_url": "ws://127.0.0.1:3001", "token": "",
            "bot_qq": 222, "runtime": {"api_timeout_seconds": 6},
        })
        image = [{"type": "image", "data": {"file": "https://example.com/a.jpg"}}]
        with patch.object(client, "call", new=AsyncMock(
                return_value={"status": "ok"})) as call_mock:
            await client.send_group_msg(100, image)
        call_mock.assert_awaited_once_with(
            "send_group_msg", {"group_id": 100, "message": image}, timeout=60)
        with patch.object(client, "call", new=AsyncMock(
                return_value={"status": "ok"})) as call_mock:
            await client.send_private_msg(200, image)
        call_mock.assert_awaited_once_with(
            "send_private_msg", {"user_id": 200, "message": image}, timeout=60)

    async def test_history_omits_unknown_message_sequence_and_caches_incompatibility(self):
        client = OneBotClient({
            "ws_url": "ws://127.0.0.1:3001", "token": "",
            "bot_qq": 222, "runtime": {},
        })
        with patch.object(client, "call", new=AsyncMock(return_value={
                "status": "failed", "message": "消息undefined不存在"})) as call_mock:
            await client.get_group_msg_history(100, count=10)
        params = call_mock.await_args.args[1]
        self.assertNotIn("message_seq", params)
        second = await client.get_group_msg_history(100, count=10)
        self.assertEqual(second["data"]["messages"], [])
        self.assertEqual(call_mock.await_count, 1)

    async def test_bot_admin_loss_notifies_owner_once_per_window(self):
        from bot.events.notice import handle_group_admin

        client = type("Client", (), {"send_private_msg": AsyncMock()})()
        dispatcher = type("DispatcherStub", (), {
            "config": {"bot_qq": 222, "bot_owner": 111}, "client": client,
        })()
        event = {"group_id": 333, "user_id": 222, "sub_type": "unset"}
        await handle_group_admin(dispatcher, event)
        await handle_group_admin(dispatcher, event)
        client.send_private_msg.assert_awaited_once()

    async def test_bot_qq_has_master_level(self):
        from bot.permission import get_user_level, LEVEL_SUPER

        class Client:
            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                raise AssertionError("bot_qq should short-circuit before API calls")

        dispatcher = Dispatcher(
            {"runtime": {}, "bot_owner": 111, "bot_qq": 222, "groups": {}}, Client())
        level, _ = await get_user_level(dispatcher, 333444, 222, "member")
        self.assertEqual(level, LEVEL_SUPER)

    async def test_private_ai_gate_default_off_is_silent(self):
        from unittest.mock import AsyncMock

        class Client:
            session = None

            def __init__(self):
                self.sent = []

            async def send_private_msg(self, user_id, message):
                self.sent.append((user_id, message))
                return {"status": "ok"}

        client = Client()
        dispatcher = Dispatcher({
            "runtime": {}, "bot_owner": 111, "bot_qq": 222,
            "private_chat": {"enabled": False, "allowed_users": []},
        }, client)
        with patch("bot.ai.handle_ai_chat", new=AsyncMock(return_value=True)) as ai_mock:
            await dispatcher._handle_private_ai_chat(
                333, [], "在吗", {"nickname": "路人"}, 0)
        ai_mock.assert_not_called()
        self.assertEqual(client.sent, [], "non-friend / disabled gate must stay silent")

    async def test_disabled_private_is_dropped_before_logging_or_routing(self):
        from unittest.mock import AsyncMock

        dispatcher = Dispatcher({
            "runtime": {}, "bot_owner": 111, "bot_qq": 222,
            "private_chat": {"enabled": False, "allowed_users": []},
            "groups": {},
        }, object())
        event = {
            "post_type": "message", "message_type": "private",
            "user_id": 333, "message_id": 1,
            "raw_message": "这条私聊不应该被处理",
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "sender": {"nickname": "路人"},
        }
        with patch("bot.dispatcher._log_chat_message") as log_mock, \
                patch.object(dispatcher, "_handle_private_ai_chat",
                             new=AsyncMock()) as private_mock:
            await dispatcher._handle_message(event)
        log_mock.assert_not_called()
        private_mock.assert_not_called()
        self.assertNotIn(1, dispatcher._seen_msg_ids)

    async def test_delayed_reply_is_cancelled_after_group_disable(self):
        from unittest.mock import AsyncMock
        from bot import ai as ai_module

        dispatcher = Dispatcher({
            "runtime": {}, "bot_owner": 111, "bot_qq": 222,
            "groups": {"9001": {"enabled": False}},
        }, object())
        with patch.object(ai_module, "handle_ai_chat",
                          new=AsyncMock()) as ai_mock:
            await dispatcher._trigger_delayed_reply(
                9001, 333, 3, [], "稍后再回复", "路人")
        ai_mock.assert_not_called()

    async def test_disabled_group_is_dropped_before_content_pipeline(self):
        from unittest.mock import AsyncMock

        dispatcher = Dispatcher({
            "runtime": {}, "bot_owner": 111, "bot_qq": 222,
            "command_prefix": "/", "private_chat": {"enabled": False},
            "groups": {"9001": {"enabled": False}},
        }, object())
        event = {
            "post_type": "message", "message_type": "group",
            "group_id": 9001, "user_id": 333, "message_id": 2,
            "raw_message": "@小汐 这条群消息不应该被处理",
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "sender": {"nickname": "路人", "role": "member"},
        }
        with patch("bot.dispatcher._log_chat_message") as log_mock, \
                patch.object(dispatcher, "_handle_group_message",
                             new=AsyncMock()) as group_mock:
            await dispatcher._handle_message(event)
        log_mock.assert_not_called()
        group_mock.assert_not_called()
        self.assertNotIn(2, dispatcher._seen_msg_ids)

    async def test_private_ai_gate_allowlist_bypasses_switch(self):
        from unittest.mock import AsyncMock

        class Client:
            session = None

            def __init__(self):
                self.sent = []

            async def call(self, action, params):
                if action == "get_friend_list":
                    return {"status": "ok", "data": [{"user_id": 333}]}
                return {"status": "ok"}

            async def send_private_msg(self, user_id, message):
                self.sent.append((user_id, message))
                return {"status": "ok"}

        client = Client()
        dispatcher = Dispatcher({
            "runtime": {}, "bot_owner": 111, "bot_qq": 222,
            "private_chat": {"enabled": False, "allowed_users": [333]},
        }, client)
        with patch("bot.ai.handle_ai_chat", new=AsyncMock(return_value=True)) as ai_mock:
            await dispatcher._handle_private_ai_chat(
                333, [], "在吗", {"nickname": "小明"}, 0)
        ai_mock.assert_called_once()

    def test_title_request_trigger(self):
        from bot.natural_triggers import check_natural_triggers
        self.assertEqual(
            check_natural_triggers("我要头衔小可爱", []),
            ("mytitle", {"title": "小可爱"}),
        )
        self.assertIsNone(check_natural_triggers("小汐我要头衔xx", []))
        self.assertIsNone(check_natural_triggers("我要头衔", []))

    async def test_my_title_silent_when_bot_not_owner(self):
        from bot.commands import cmd_my_title

        class Client:
            def __init__(self):
                self.title_calls = []

            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                return {"status": "ok", "data": {"role": "member"}}

            async def set_group_special_title(self, group_id, user_id, title=""):
                self.title_calls.append((group_id, user_id, title))
                return {"status": "ok"}

        replies = []

        class DispatcherStub:
            config = {"bot_qq": 222}
            client = Client()

            async def _reply(self, group_id, user_id, text):
                replies.append(text)

        d = DispatcherStub()
        await cmd_my_title(d, 998001, 333, "小可爱", "member", "小明", [])
        self.assertEqual(replies, [], "must stay silent when bot is not group owner")
        self.assertEqual(d.client.title_calls, [])

if __name__ == "__main__":
    unittest.main()


class PermissionTierTests(unittest.IsolatedAsyncioTestCase):
    """Five-tier permission system: super > master > gowner > admin > member."""

    def _dispatcher(self, role="member"):
        from bot import permission

        class Client:
            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                return {"status": "ok", "data": {"role": role}}

        class Stub:
            config = {
                "bot_owner": 111,
                "bot_qq": 222,
                "group_defaults": {},
                "groups": {"100": {"enabled": True, "masters": [333]}},
            }
            client = Client()

        return Stub(), permission

    async def test_user_levels(self):
        from bot.permission import (LEVEL_SUPER, LEVEL_MASTER, LEVEL_GOWNER,
                                    LEVEL_ADMIN, LEVEL_MEMBER)
        # super: bot owner and bot account
        stub, permission = self._dispatcher("member")
        level, _ = await permission.get_user_level(stub, 100, 111)
        self.assertEqual(level, LEVEL_SUPER)
        level, _ = await permission.get_user_level(stub, 100, 222)
        self.assertEqual(level, LEVEL_SUPER)
        # group master
        level, _ = await permission.get_user_level(stub, 100, 333)
        self.assertEqual(level, LEVEL_MASTER)
        # QQ group owner -> gowner (not admin)
        stub, permission = self._dispatcher("owner")
        level, _ = await permission.get_user_level(stub, 100, 444)
        self.assertEqual(level, LEVEL_GOWNER)
        # QQ admin -> admin
        stub, permission = self._dispatcher("admin")
        level, _ = await permission.get_user_level(stub, 100, 555)
        self.assertEqual(level, LEVEL_ADMIN)
        # plain member
        stub, permission = self._dispatcher("member")
        level, _ = await permission.get_user_level(stub, 100, 666)
        self.assertEqual(level, LEVEL_MEMBER)

    async def test_bot_account_may_use_owner_only_commands(self):
        stub, permission = self._dispatcher("member")
        cmd_info = {"bot_owner_only": True}
        allowed, _ = await permission.check_permission(stub, 100, 222, "member", cmd_info)
        self.assertTrue(allowed)
        allowed, _ = await permission.check_permission(stub, 100, 999, "member", cmd_info)
        self.assertFalse(allowed)
        allowed, _ = await permission.check_permission(stub, 100, 333, "member", cmd_info)
        self.assertFalse(allowed)  # group master is not super

    async def test_admin_only_allows_gowner(self):
        stub, permission = self._dispatcher("owner")
        cmd_info = {"admin_only": True}
        allowed, _ = await permission.check_permission(stub, 100, 444, "owner", cmd_info)
        self.assertTrue(allowed)

    async def test_moderation_hierarchy(self):
        stub, permission = self._dispatcher("member")
        # group master cannot moderate super (bot owner)
        allowed, _ = await permission.can_moderate_target(stub, 100, 333, 111)
        self.assertFalse(allowed)
        # super (bot owner) can moderate group master
        allowed, _ = await permission.can_moderate_target(stub, 100, 111, 333)
        self.assertTrue(allowed)
        # group master can moderate plain member
        allowed, _ = await permission.can_moderate_target(stub, 100, 333, 666)
        self.assertTrue(allowed)
        # member cannot moderate group master
        allowed, _ = await permission.can_moderate_target(stub, 100, 666, 333)
        self.assertFalse(allowed)


class ModerationMatrixTests(unittest.IsolatedAsyncioTestCase):
    """Permission matrix: a group master whose QQ role is plain member must be
    able to use ban/kick on QQ admins when the bot itself is admin/owner."""

    OWNER = 111
    BOT = 222
    MASTER = 333   # QQ role member, listed in group masters
    GOWNER = 444   # QQ group owner
    ADMIN = 555    # QQ admin
    MEMBER = 666   # plain member
    GROUP = 100
    BAN_CMD = {"admin_only": True, "bot_admin_required": True}

    def _dispatcher(self, roles=None, bot_role="owner", masters=None):
        """roles: {user_id: qq_role}; bot_role: bot's QQ role in the group."""
        from bot import permission
        permission._bot_role_cache.clear()
        roles = roles or {}
        bot_qq = self.BOT
        masters = [self.MASTER] if masters is None else masters

        class Client:
            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                if user_id == bot_qq:
                    return {"status": "ok", "data": {"role": bot_role}}
                return {"status": "ok", "data": {"role": roles.get(user_id, "member")}}

        class Stub:
            config = {
                "bot_owner": 111,
                "bot_qq": 222,
                "group_defaults": {},
                "groups": {"100": {"enabled": True, "masters": masters}},
            }
            client = Client()

        return Stub(), permission

    # ---- can_moderate_target matrix ----

    async def test_master_can_moderate_qq_admin(self):
        stub, permission = self._dispatcher(roles={self.ADMIN: "admin"})
        allowed, err = await permission.can_moderate_target(
            stub, self.GROUP, self.MASTER, self.ADMIN, "member")
        self.assertTrue(allowed, err)

    async def test_gowner_can_moderate_qq_admin(self):
        stub, permission = self._dispatcher(
            roles={self.GOWNER: "owner", self.ADMIN: "admin"})
        allowed, err = await permission.can_moderate_target(
            stub, self.GROUP, self.GOWNER, self.ADMIN, "owner")
        self.assertTrue(allowed, err)

    async def test_admin_cannot_moderate_peer_or_master(self):
        other_admin = 556
        stub, permission = self._dispatcher(
            roles={self.ADMIN: "admin", other_admin: "admin"})
        allowed, _ = await permission.can_moderate_target(
            stub, self.GROUP, self.ADMIN, other_admin, "admin")
        self.assertFalse(allowed)  # same level
        allowed, _ = await permission.can_moderate_target(
            stub, self.GROUP, self.ADMIN, self.MASTER, "admin")
        self.assertFalse(allowed)  # master ranks above admin

    async def test_member_cannot_moderate_anyone(self):
        other_member = 667
        stub, permission = self._dispatcher(
            roles={self.GOWNER: "owner", self.ADMIN: "admin"})
        for target in (other_member, self.ADMIN, self.GOWNER, self.MASTER):
            allowed, _ = await permission.can_moderate_target(
                stub, self.GROUP, self.MEMBER, target, "member")
            self.assertFalse(allowed, "member must not moderate %s" % target)

    async def test_bot_owner_can_moderate_anyone(self):
        stub, permission = self._dispatcher(
            roles={self.GOWNER: "owner", self.ADMIN: "admin"})
        # bot_owner can moderate everyone except the QQ group owner
        # (QQ itself forbids operating on the group owner).
        for target in (self.MASTER, self.ADMIN, self.MEMBER):
            allowed, err = await permission.can_moderate_target(
                stub, self.GROUP, self.OWNER, target)
            self.assertTrue(allowed, "bot owner must moderate %s: %s" % (target, err))
        allowed, _ = await permission.can_moderate_target(
            stub, self.GROUP, self.OWNER, self.GOWNER)
        self.assertFalse(allowed, "even bot owner must not moderate the QQ group owner")

    async def test_protected_targets(self):
        stub, permission = self._dispatcher(roles={self.ADMIN: "admin"})
        for actor in (self.OWNER, self.MASTER, self.ADMIN):
            for target in (self.OWNER, self.BOT):
                allowed, _ = await permission.can_moderate_target(
                    stub, self.GROUP, actor, target)
                self.assertFalse(
                    allowed, "%s must not moderate protected %s" % (actor, target))

    # ---- check_permission matrix for ban/kick-style commands ----

    async def test_master_member_allowed_ban_when_bot_is_owner(self):
        stub, permission = self._dispatcher(bot_role="owner")
        allowed, err = await permission.check_permission(
            stub, self.GROUP, self.MASTER, "member", self.BAN_CMD)
        self.assertTrue(allowed, err)

    async def test_qq_admin_allowed_ban_when_bot_is_owner(self):
        stub, permission = self._dispatcher(
            roles={self.ADMIN: "admin"}, bot_role="owner")
        allowed, err = await permission.check_permission(
            stub, self.GROUP, self.ADMIN, "admin", self.BAN_CMD)
        self.assertTrue(allowed, err)

    async def test_plain_member_denied_ban(self):
        stub, permission = self._dispatcher(bot_role="owner")
        allowed, _ = await permission.check_permission(
            stub, self.GROUP, self.MEMBER, "member", self.BAN_CMD)
        self.assertFalse(allowed)

    async def test_admin_denied_ban_when_bot_not_admin(self):
        stub, permission = self._dispatcher(
            roles={self.ADMIN: "admin"}, bot_role="member")
        allowed, _ = await permission.check_permission(
            stub, self.GROUP, self.ADMIN, "admin", self.BAN_CMD)
        self.assertFalse(allowed)

    async def test_member_denied_ban_when_bot_not_admin(self):
        stub, permission = self._dispatcher(bot_role="member")
        allowed, _ = await permission.check_permission(
            stub, self.GROUP, self.MEMBER, "member", self.BAN_CMD)
        self.assertFalse(allowed)


class BilibiliHelperTests(unittest.TestCase):
    def test_mixin_key_known_vector(self):
        from bot.bilibili import mixin_key
        self.assertEqual(
            mixin_key("7cd084941338484aae1ad9425b84077c",
                      "4932caff0ff746eab6f01bf08b70ac45"),
            "ea1db124af3c7062474693fa704f4ff8",
        )

    def test_wbi_sign_strips_specials_and_adds_rid(self):
        from bot.bilibili import wbi_sign
        signed = wbi_sign({"mid": 2, "foo": "a!'()*b"},
                          "7cd084941338484aae1ad9425b84077c",
                          "4932caff0ff746eab6f01bf08b70ac45")
        self.assertEqual(signed["foo"], "ab")
        self.assertEqual(len(signed["w_rid"]), 32)
        self.assertIn("wts", signed)

    def test_extract_video_ids(self):
        from bot.bilibili import extract_bvid, extract_av, extract_b23
        self.assertEqual(extract_bvid("看看这个 BV1GJ411x7h7 真好笑"), "BV1GJ411x7h7")
        self.assertEqual(extract_bvid("没有BV号"), "")
        self.assertEqual(extract_av("av80433022"), 80433022)
        self.assertEqual(extract_b23("来 https://b23.tv/abc123 看看"),
                         "https://b23.tv/abc123")

    def test_format_helpers(self):
        from bot.bilibili import format_duration, format_count, format_video_text
        self.assertEqual(format_duration(213), "3:33")
        self.assertEqual(format_count(526439), "52.6万")
        self.assertEqual(format_count(999), "999")
        text = format_video_text(
            {"title": "标题", "duration": 61, "desc": "简介",
             "owner": {"name": "UP"}, "stat": {"view": 1, "danmaku": 2, "like": 3}},
            "https://www.bilibili.com/video/BV1GJ411x7h7")
        self.assertIn("标题", text)
        self.assertIn("1:01", text)
        self.assertIn("BV1GJ411x7h7", text)

    def test_push_state_marks_and_dedups(self):
        from bot import bilibili
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "push.json")
            with patch.object(bilibili, "_PUSH_STATE_PATH", path):
                bilibili.reset_state_for_test()
                bilibili.mark_pushed(100, 42, ["BV1", "BV2"])
                bilibili.mark_pushed(100, 42, ["BV2", "BV3"])
                seen = bilibili.pushed_bvids(100, 42)
                self.assertEqual(seen, ["BV1", "BV2", "BV3"])
                bilibili.reset_state_for_test()


class UapiBudgetTests(unittest.TestCase):
    def _config(self):
        return {"uapi": {"daily_limit": 100, "reserve": 30, "month_limit": 3400},
                "uapi_api_key": "k"}

    def test_daily_split_and_block(self):
        from bot import uapi
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(uapi, "_STATE_PATH", os.path.join(tmp, "s.json")):
                uapi.reset_state_for_test()
                config = self._config()
                # user bucket = 70, weather costs 2 -> 35 calls then blocked
                for _ in range(35):
                    self.assertTrue(uapi._charge(config, "/misc/weather", "user"))
                self.assertFalse(uapi.credits_available(config, "user"))
                # auto bucket still available
                self.assertTrue(uapi.credits_available(config, "auto"))
                for _ in range(30):
                    self.assertTrue(uapi._charge(config, "/misc/hotboard", "auto"))
                self.assertFalse(uapi.credits_available(config, "auto"))
                # free endpoint never blocked
                self.assertTrue(uapi._charge(config, "/random/image", "user"))
                info = uapi.credits_remaining(config)
                self.assertEqual(info["user_left"], 0)
                self.assertEqual(info["auto_left"], 0)
                uapi.reset_state_for_test()

    def test_day_rollover_resets_counters(self):
        from bot import uapi
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(uapi, "_STATE_PATH", os.path.join(tmp, "s.json")):
                uapi.reset_state_for_test()
                config = self._config()
                self.assertTrue(uapi._charge(config, "/misc/weather", "user"))
                state = uapi._load_state()
                state["date"] = "2000-01-01"  # force stale date
                self.assertTrue(uapi.credits_available(config, "user"))
                self.assertEqual(uapi._load_state()["day_user"], 0)
                uapi.reset_state_for_test()

    def test_legacy_pre_response_header_counters_are_reset(self):
        from bot import uapi
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "date": uapi._today(), "month": uapi._month(),
                    "day_user": 50, "day_auto": 20, "month_used": 999,
                }, handle)
            with patch.object(uapi, "_STATE_PATH", path):
                uapi.reset_state_for_test()
                state = uapi._load_state()
                self.assertEqual(state["accounting_version"], 2)
                self.assertEqual(state["day_user"], 0)
                self.assertEqual(state["day_auto"], 0)
                self.assertEqual(state["month_used"], 0)
                uapi.reset_state_for_test()

    def test_month_limit_blocks_all(self):
        from bot import uapi
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(uapi, "_STATE_PATH", os.path.join(tmp, "s.json")):
                uapi.reset_state_for_test()
                config = self._config()
                state = uapi._load_state()
                state["month"] = uapi._month()
                state["month_used"] = 3400
                self.assertFalse(uapi.credits_available(config, "user"))
                self.assertFalse(uapi.credits_available(config, "auto"))
                self.assertTrue(uapi._charge(config, "/random/image", "user"))
                uapi.reset_state_for_test()

    def test_missing_key_uses_visitor_quota_and_rate_limits_log(self):
        from bot import uapi

        class Response:
            status = 200
            headers = {"Uapi-Credits-Charged": "0"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, content_type=None):
                return {"answer": "ok"}

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append(kwargs)
                return Response()

        class Client:
            session = Session()

        class Stub:
            config = {}
            client = Client()

        uapi.reset_state_for_test()
        with patch("bot.integrations.uapi.log.info") as info, \
                patch("bot.integrations.uapi.log.debug") as debug:
            asyncio.run(uapi.uapi_get(Stub(), "/answerbook/ask"))
            asyncio.run(uapi.uapi_get(Stub(), "/answerbook/ask"))
            asyncio.run(uapi.uapi_get(Stub(), "/image/bing-daily"))
        self.assertEqual(info.call_count, 2)
        debug.assert_called_once()
        self.assertEqual([call["headers"] for call in Stub.client.session.calls], [{}, {}, {}])
        uapi.reset_state_for_test()

    def test_refresh_official_quota_uses_free_header_endpoint(self):
        from bot import uapi

        class Stub:
            config = self._config()

        async def fake_request(dispatcher, method, path, **kwargs):
            self.assertIs(dispatcher, stub)
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/saying")
            self.assertEqual(kwargs["kind"], "user")
            return {"text": "ok"}

        stub = Stub()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(uapi, "_STATE_PATH", os.path.join(tmp, "s.json")), \
                    patch.object(uapi, "_json_request", side_effect=fake_request) as request:
                uapi.reset_state_for_test()
                info = asyncio.run(uapi.refresh_official_quota(stub))
        request.assert_awaited_once()
        self.assertIsNone(info["official_month_remaining"])
        uapi.reset_state_for_test()

    def test_response_headers_are_authoritative_for_charge_and_quota(self):
        from bot import uapi

        class Response:
            status = 200
            headers = {
                "Uapi-Credits-Charged": "1",
                "Ratelimit": '"billing-key-rate";r=6,"billing-quota";r=3490;uapi-unit="credits"',
                "Ratelimit-Policy": '"billing-key-rate";q=7,"billing-quota";q=3500;uapi-unit="credits"',
            }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, content_type=None):
                return {"list": []}

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        class Client:
            session = Session()

        class Stub:
            config = self._config()
            client = Client()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(uapi, "_STATE_PATH", os.path.join(tmp, "s.json")):
                uapi.reset_state_for_test()
                asyncio.run(uapi.uapi_get(Stub(), "/misc/hotboard"))
                info = uapi.credits_remaining(Stub.config)
        self.assertEqual(info["day_used"], 1)
        self.assertEqual(info["official_month_remaining"], 3490)
        self.assertEqual(info["official_month_limit"], 3500)
        uapi.reset_state_for_test()


class ShortVoiceReplyTests(unittest.IsolatedAsyncioTestCase):
    def _stub(self, client, probability=1.0, enabled=True):
        class Stub:
            config = {
                "voice_reply": {
                    "enabled": True,
                    "probability": probability,
                    "min_chars": 5,
                    "max_chars": 45,
                    "cooldown_seconds": 3600,
                    "daily_limit": 2,
                    "character_id": "lucy-voice-xueling",
                },
                "group_defaults": {"features": {"voice_reply": enabled}},
                "groups": {"100": {"enabled": True, "features": {}}},
            }
        stub = Stub()
        stub.client = client
        return stub

    async def test_short_voice_success_obeys_cooldown(self):
        from bot.services import voice_reply

        class Client:
            def __init__(self):
                self.calls = []

            async def send_group_ai_record(self, group_id, character, text):
                self.calls.append((group_id, character, text))
                return {"status": "ok", "retcode": 0}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "voice.json")
            client = Client()
            stub = self._stub(client)
            with patch.object(voice_reply, "_STATE_PATH", path), \
                    patch.object(voice_reply.random, "random", return_value=0):
                voice_reply.reset_state_for_test()
                self.assertTrue(await voice_reply.maybe_send_short_voice(
                    stub, 100, "????????"))
                self.assertFalse(await voice_reply.maybe_send_short_voice(
                    stub, 100, "????????"))
                state = voice_reply._load_state()
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0][1], "lucy-voice-xueling")
            self.assertEqual(state["groups"]["100"]["count"], 1)
            voice_reply.reset_state_for_test()

    async def test_voice_rejects_links_long_text_and_disabled_group(self):
        from bot.services import voice_reply

        class Client:
            async def send_group_ai_record(self, group_id, character, text):
                raise AssertionError("ineligible text must not call NapCat")

        voice_reply.reset_state_for_test()
        stub = self._stub(Client())
        self.assertFalse(await voice_reply.maybe_send_short_voice(
            stub, 100, "?? https://example.com"))
        self.assertFalse(await voice_reply.maybe_send_short_voice(
            stub, 100, "??" * 30))
        disabled = self._stub(Client(), enabled=False)
        self.assertFalse(await voice_reply.maybe_send_short_voice(
            disabled, 100, "????????"))
        voice_reply.reset_state_for_test()


class InteractionQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_cap_and_reset(self):
        import ai_tools

        class Client:
            async def set_msg_emoji_like(self, message_id, emoji_id):
                return {"status": "ok", "retcode": 0}

            async def send_like(self, user_id, times=10):
                return {"status": "ok", "retcode": 0}

        class Stub:
            client = Client()
            config = {}

        ai_tools.reset_quota_for_test()
        stub = Stub()
        self.assertEqual(ai_tools.interaction_quota_left(100),
                         ai_tools.INTERACTION_DAILY_LIMIT)
        for _ in range(ai_tools.INTERACTION_DAILY_LIMIT):
            result = await ai_tools.execute_interaction_tool(
                stub, "set_msg_emoji_like", {"message_id": 1}, group_id=100, user_id=7)
            self.assertTrue(result["ok"])
        self.assertEqual(ai_tools.interaction_quota_left(100), 0)
        result = await ai_tools.execute_interaction_tool(
            stub, "send_like", {"user_id": 7}, group_id=100, user_id=7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "interaction_quota_exhausted")
        # unknown tool rejected
        result = await ai_tools.execute_interaction_tool(
            stub, "set_group_kick", {}, group_id=100, user_id=7)
        self.assertEqual(result["error"], "interaction_tool_not_allowed")
        ai_tools.reset_quota_for_test()


class SchedulerJobTests(unittest.TestCase):
    def test_scheduled_jobs_include_all(self):
        class Stub:
            config = {
                "groups": {},
                "acg_images": {"enabled": True, "times": [0, 6, 12, 18]},
                "hotboard_push": {"enabled": True, "times": [9, 21]},
            }

        # Pin "now" to 01:00 in the scheduler timezone so every randomized
        # slot later today is still pending regardless of wall-clock time.
        fixed_now = datetime.now(scheduler._timezone()).replace(
            hour=1, minute=0, second=0, microsecond=0).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path), \
                    patch.object(scheduler.time, "time", return_value=fixed_now):
                jobs = scheduler._scheduled_jobs(Stub())
        names = {job[0] for job in jobs}
        self.assertIn("checkin", names)
        self.assertIn("acg", names)
        self.assertIn("hotboard", names)

    def test_random_schedule_is_persisted(self):
        class Stub:
            config = {
                "runtime": {"scheduler_timezone": "Asia/Shanghai"},
                "acg_images": {"enabled": True},
                "hotboard_push": {"enabled": True},
            }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path):
                scheduler._scheduled_jobs(Stub())
                first = scheduler._load_acg_state()["schedule"]
                scheduler._scheduled_jobs(Stub())
                second = scheduler._load_acg_state()["schedule"]
        self.assertEqual(first, second)
        self.assertEqual(len(first["acg"]), 4)
        self.assertEqual(len(first["hotboard"]), 2)

    def test_format_hotboard(self):
        text = scheduler.format_hotboard(
            "bilibili",
            [{"title": "视频A", "hot_value": "100万"},
             {"title": "视频B", "hot_value": ""}])
        self.assertIn("【B站热榜】", text)
        self.assertIn("1. 视频A（100万）", text)
        self.assertIn("2. 视频B", text)

    def test_tool_gate_new_keywords(self):
        self.assertTrue(_should_consider_napcat_tool("查一下杭州天气"))
        self.assertTrue(_should_consider_napcat_tool("今天热搜有什么"))
        self.assertTrue(_should_consider_napcat_tool("给我翻下答案之书"))
        self.assertFalse(_should_consider_napcat_tool("今天晚饭吃啥好"))


class BiliPushWatermarkTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, tmp):
        from bot import bilibili
        bilibili.reset_state_for_test()
        self._patcher = patch.object(
            bilibili, "_PUSH_STATE_PATH", os.path.join(tmp, "push.json"))
        self._patcher.start()
        self.bilibili = bilibili

    def tearDown(self):
        if hasattr(self, "_patcher"):
            self._patcher.stop()
        if hasattr(self, "bilibili"):
            self.bilibili.reset_state_for_test()

    async def test_watermark_blocks_stale_fallback_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._setup(tmp)
            bilibili = self.bilibili
            sent = []

            class Client:
                _running = True

                async def send_group_msg(self, group_id, message):
                    sent.append((group_id, message))
                    return {"status": "ok", "retcode": 0}

                async def get_group_member_info(self, group_id, user_id, no_cache=False):
                    return {"status": "ok", "data": {"role": "member"}}

            class Stub:
                config = {
                    "bot_qq": 222,
                    "groups": {"100": {"enabled": True,
                                       "bili_push": {"mids": [42]}}},
                }
                client = Client()

            videos = [
                {"bvid": "BV_new", "title": "新", "created": 2000},
                {"bvid": "BV_old1", "title": "旧1", "created": 1000},
                {"bvid": "BV_old2", "title": "旧2", "created": 500},
            ]
            # watermark at 1500: only BV_new may be announced
            bilibili.mark_pushed(100, 42, [], watermark=1500)

            async def fake_archives(dispatcher, mid, count=5):
                return videos

            with patch.object(bilibili, "get_archives", fake_archives):
                announced = await bilibili.poll_once(Stub())
            self.assertEqual(announced, 1)
            self.assertEqual(len(sent), 1)
            # watermark advanced past announced video
            self.assertEqual(bilibili.push_watermark(100, 42), 2000)
            # second round: nothing new
            with patch.object(bilibili, "get_archives", fake_archives):
                announced = await bilibili.poll_once(Stub())
            self.assertEqual(announced, 0)

    async def test_prime_sets_watermark_to_now_when_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._setup(tmp)
            bilibili = self.bilibili

            class Stub:
                config = {}
                client = None

            async def failing_archives(dispatcher, mid, count=5):
                return []

            with patch.object(bilibili, "get_archives", failing_archives):
                videos = await bilibili.prime_push_state(Stub(), 100, 42)
            self.assertEqual(videos, [])
            self.assertGreaterEqual(bilibili.push_watermark(100, 42),
                                    1700000000)  # ~now, not 0

    def test_old_list_format_migrates(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "push.json")
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"100": {"42": ["BV1", "BV2"]}}, f)
            self._setup(tmp)
            bilibili = self.bilibili
            self.assertEqual(bilibili.pushed_bvids(100, 42), ["BV1", "BV2"])
            self.assertEqual(bilibili.push_watermark(100, 42), 0)
            bilibili.mark_pushed(100, 42, ["BV3"], watermark=99)
            self.assertEqual(bilibili.pushed_bvids(100, 42),
                             ["BV1", "BV2", "BV3"])
            self.assertEqual(bilibili.push_watermark(100, 42), 99)

    async def test_timeout_confirmed_by_history_counts_as_success(self):
        from bot import bilibili

        class Client:
            def __init__(self):
                self.history_calls = 0

            async def get_group_msg_history(self, group_id, count=30):
                self.history_calls += 1
                messages = []
                if self.history_calls >= 2:
                    messages = [{
                        "user_id": 222,
                        "raw_message": "https://www.bilibili.com/video/BV_confirmed",
                    }]
                return {"status": "ok", "data": {"messages": messages}}

            async def send_group_msg(self, group_id, message):
                return {"status": "timeout", "error_kind": "timeout"}

        class Stub:
            config = {"bot_qq": 222}
            client = Client()

        with patch("bot.bilibili.asyncio.sleep", new=AsyncMock()):
            result = await bilibili._send_group_confirmed(
                Stub(), 100, [], "BV_confirmed", "bili video")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_by"], "history_after_timeout")

    async def test_timeout_confirmation_retries_for_delayed_history(self):
        from bot import bilibili

        class Client:
            def __init__(self):
                self.history_calls = 0

            async def get_group_msg_history(self, group_id, count=30):
                self.history_calls += 1
                messages = []
                if self.history_calls >= 4:
                    messages = [{
                        "user_id": 222,
                        "raw_message": "https://www.bilibili.com/video/BV_delayed",
                    }]
                return {"status": "ok", "data": {"messages": messages}}

            async def send_group_msg(self, group_id, message):
                return {"status": "timeout", "error_kind": "timeout"}

        class Stub:
            config = {"bot_qq": 222}
            client = Client()

        with patch("bot.bilibili.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await bilibili._send_group_confirmed(
                Stub(), 100, [], "BV_delayed", "bili video")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(Stub.client.history_calls, 4)
        self.assertEqual(sleep.await_count, 3)

    async def test_unconfirmed_item_enters_delivery_backoff(self):
        from bot import bilibili

        class Client:
            def __init__(self):
                self.send_calls = 0

            async def get_group_msg_history(self, group_id, count=30):
                return {"status": "ok", "data": {"messages": []}}

            async def send_group_msg(self, group_id, message):
                self.send_calls += 1
                return {"status": "timeout", "error_kind": "timeout"}

        dispatcher = type("Stub", (), {
            "config": {"bot_qq": 222},
            "client": Client(),
        })()
        with patch("bot.bilibili.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await bilibili._send_group_confirmed(
                    dispatcher, 100, [], "BV_backoff", "bili video")
            with self.assertRaises(bilibili.BiliDeliveryDeferred):
                await bilibili._send_group_confirmed(
                    dispatcher, 100, [], "BV_backoff", "bili video")
        self.assertEqual(dispatcher.client.send_calls, 1)

    async def test_unconfirmed_timeout_does_not_advance_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._setup(tmp)
            bilibili = self.bilibili

            class Client:
                _running = True

                async def get_group_msg_history(self, group_id, count=30):
                    return {"status": "ok", "data": {"messages": []}}

                async def send_group_msg(self, group_id, message):
                    return {"status": "timeout", "error_kind": "timeout"}

                async def get_group_member_info(self, group_id, user_id,
                                                no_cache=False):
                    return {"status": "ok", "data": {"role": "member"}}

            class Stub:
                config = {
                    "bot_qq": 222,
                    "groups": {"100": {"enabled": True,
                                          "bili_push": {"mids": [42]}}},
                }
                client = Client()

            bilibili.mark_pushed(100, 42, [], watermark=1000)

            async def fake_archives(dispatcher, mid, count=5):
                return [{"bvid": "BV_failed", "title": "新", "created": 2000}]

            with patch.object(bilibili, "get_archives", fake_archives), \
                    patch("bot.bilibili.asyncio.sleep", new=AsyncMock()):
                announced = await bilibili.poll_once(Stub())
            self.assertEqual(announced, 0)
            self.assertEqual(bilibili.push_watermark(100, 42), 1000)
            self.assertNotIn("BV_failed", bilibili.pushed_bvids(100, 42))


class BiliDynamicsTests(unittest.TestCase):
    def _opus_item(self, dyn_id="111", mid=42, name="UP主", ts=2000,
                   text="今天画了新图", pics=None, item_type="DYNAMIC_TYPE_DRAW"):
        major = {"opus": {"summary": {"text": text},
                          "pics": [{"url": u} for u in (pics or [])]}}
        return {
            "id_str": dyn_id, "type": item_type,
            "modules": {
                "module_author": {"mid": mid, "name": name, "pub_ts": ts},
                "module_dynamic": {"major": major},
            },
        }

    def test_parse_draw_dynamic(self):
        from bot.bilibili import parse_dynamic_item
        dyn = parse_dynamic_item(self._opus_item(pics=["https://i0.hdslb.com/a.jpg"]))
        self.assertEqual(dyn["id"], "111")
        self.assertEqual(dyn["mid"], 42)
        self.assertEqual(dyn["text"], "今天画了新图")
        self.assertEqual(dyn["images"], ["https://i0.hdslb.com/a.jpg"])
        self.assertEqual(dyn["link"], "https://t.bilibili.com/111")

    def test_video_dynamics_skipped(self):
        from bot.bilibili import parse_dynamic_item
        self.assertIsNone(parse_dynamic_item(
            self._opus_item(item_type="DYNAMIC_TYPE_AV")))

    def test_empty_dynamic_skipped(self):
        from bot.bilibili import parse_dynamic_item
        self.assertIsNone(parse_dynamic_item(self._opus_item(text="")))
        self.assertIsNone(parse_dynamic_item({"type": "DYNAMIC_TYPE_WORD"}))

    def test_forward_dynamic(self):
        from bot.bilibili import parse_dynamic_item
        orig = self._opus_item(dyn_id="100", mid=7, name="原主", text="原内容")
        item = self._opus_item(dyn_id="101", text="",
                               item_type="DYNAMIC_TYPE_FORWARD")
        item["orig"] = orig
        dyn = parse_dynamic_item(item)
        self.assertEqual(dyn["id"], "101")
        self.assertEqual(dyn["text"], "转发了 @原主：原内容")


class BiliDynamicsPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamics_watermark_and_dedup(self):
        from bot import bilibili
        with tempfile.TemporaryDirectory() as tmp:
            bilibili.reset_state_for_test()
            with patch.object(bilibili, "_PUSH_STATE_PATH",
                              os.path.join(tmp, "push.json")):
                sent = []

                class Client:
                    async def send_group_msg(self, group_id, message):
                        sent.append((group_id, message))
                        return {"status": "ok", "retcode": 0}

                class Stub:
                    config = {
                        "bot_qq": 222, "bili_sessdata": "x",
                        "groups": {"100": {"enabled": True,
                                           "bili_push": {"mids": [42]}}},
                    }
                    client = Client()

                items = [{
                    "id_str": "500", "type": "DYNAMIC_TYPE_DRAW",
                    "modules": {
                        "module_author": {"mid": 42, "name": "UP", "pub_ts": 3000},
                        "module_dynamic": {"major": {
                            "opus": {"summary": {"text": "新动态"}, "pics": []}}},
                    },
                }, {
                    "id_str": "499", "type": "DYNAMIC_TYPE_DRAW",
                    "modules": {
                        "module_author": {"mid": 42, "name": "UP", "pub_ts": 1000},
                        "module_dynamic": {"major": {
                            "opus": {"summary": {"text": "旧动态"}, "pics": []}}},
                    },
                }]

                async def fake_feed(dispatcher):
                    return items

                bilibili.mark_pushed(100, 42, [], watermark=0)
                entry = bilibili._dyn_entry(100, 42)
                entry["dyn_watermark"] = 2000
                with patch.object(bilibili, "get_dynamics_feed", fake_feed):
                    announced = await bilibili.poll_dynamics_once(Stub())
                self.assertEqual(announced, 1)  # 旧动态被水印挡住
                with patch.object(bilibili, "get_dynamics_feed", fake_feed):
                    announced = await bilibili.poll_dynamics_once(Stub())
                self.assertEqual(announced, 0)  # 已推过的不重复
                bilibili.reset_state_for_test()


class BiliFeedAvTests(unittest.IsolatedAsyncioTestCase):
    def _av_item(self, dyn_id="900", mid=42, name="UP", ts=3000,
                 bvid="BV_feed1", title="新投稿"):
        return {
            "id_str": dyn_id, "type": "DYNAMIC_TYPE_AV",
            "modules": {
                "module_author": {"mid": mid, "name": name, "pub_ts": ts},
                "module_dynamic": {"major": {"archive": {
                    "bvid": bvid, "title": title,
                    "cover": "https://i0.hdslb.com/c.jpg"}}},
            },
        }

    def test_parse_av_dynamic(self):
        from bot.bilibili import parse_av_dynamic
        v = parse_av_dynamic(self._av_item(ts=1234, bvid="BV_x"))
        self.assertEqual(v["bvid"], "BV_x")
        self.assertEqual(v["created"], 1234)
        self.assertIsNone(parse_av_dynamic({"type": "DYNAMIC_TYPE_AV",
                                            "modules": {}}))

    async def test_feed_av_video_pushed_and_deduped(self):
        import time as _time
        from bot import bilibili
        with tempfile.TemporaryDirectory() as tmp:
            bilibili.reset_state_for_test()
            with patch.object(bilibili, "_PUSH_STATE_PATH",
                              os.path.join(tmp, "push.json")):
                sent = []

                class Client:
                    _running = True

                    async def send_group_msg(self, group_id, message):
                        sent.append((group_id, message))
                        return {"status": "ok", "retcode": 0}

                    async def get_group_member_info(self, group_id, user_id,
                                                    no_cache=False):
                        return {"status": "ok", "data": {"role": "member"}}

                now = int(_time.time())

                class Stub:
                    config = {
                        "bot_qq": 222, "bili_sessdata": "x",
                        "groups": {"100": {"enabled": True,
                                           "bili_push": {"mids": [42]}}},
                    }
                    client = Client()

                items = [self._av_item(dyn_id="901", ts=now - 60,
                                       bvid="BV_fresh"),
                         self._av_item(dyn_id="900", ts=now - 7200,
                                       bvid="BV_stale")]

                async def fake_feed(dispatcher):
                    return items

                # virgin entry: only the single newest fresh video, no history
                with patch.object(bilibili, "get_dynamics_feed", fake_feed):
                    announced = await bilibili.poll_dynamics_once(Stub())
                self.assertEqual(announced, 1)
                self.assertEqual(len(sent), 1)
                self.assertIn("BV_fresh", str(sent))
                self.assertNotIn("BV_stale", str(sent))
                # watermark sealed history; second round announces nothing
                with patch.object(bilibili, "get_dynamics_feed", fake_feed):
                    announced = await bilibili.poll_dynamics_once(Stub())
                self.assertEqual(announced, 0)
                bilibili.reset_state_for_test()

    async def test_virgin_dynamics_only_newest_fresh(self):
        import time as _time
        from bot import bilibili
        with tempfile.TemporaryDirectory() as tmp:
            bilibili.reset_state_for_test()
            with patch.object(bilibili, "_PUSH_STATE_PATH",
                              os.path.join(tmp, "push.json")):
                sent = []

                class Client:
                    async def send_group_msg(self, group_id, message):
                        sent.append((group_id, message))
                        return {"status": "ok", "retcode": 0}

                now = int(_time.time())

                class Stub:
                    config = {
                        "bot_qq": 222, "bili_sessdata": "x",
                        "groups": {"100": {"enabled": True,
                                           "bili_push": {"mids": [42]}}},
                    }
                    client = Client()

                def draw(dyn_id, ts, text):
                    return {
                        "id_str": dyn_id, "type": "DYNAMIC_TYPE_DRAW",
                        "modules": {
                            "module_author": {"mid": 42, "name": "UP",
                                              "pub_ts": ts},
                            "module_dynamic": {"major": {
                                "opus": {"summary": {"text": text},
                                         "pics": []}}},
                        },
                    }

                items = [draw("601", now - 120, "新鲜动态"),
                         draw("600", now - 300, "稍早动态"),
                         draw("599", now - 86400, "昨天动态")]

                async def fake_feed(dispatcher):
                    return items

                with patch.object(bilibili, "get_dynamics_feed", fake_feed):
                    announced = await bilibili.poll_dynamics_once(Stub())
                # virgin: at most one, the newest fresh one
                self.assertEqual(announced, 1)
                self.assertIn("新鲜动态", str(sent))
                self.assertNotIn("昨天动态", str(sent))
                # history sealed even though un-announced
                entry = bilibili._dyn_entry(100, 42)
                self.assertEqual(entry["dyn_watermark"], now - 120)
                with patch.object(bilibili, "get_dynamics_feed", fake_feed):
                    announced = await bilibili.poll_dynamics_once(Stub())
                self.assertEqual(announced, 0)
                bilibili.reset_state_for_test()


class ShareCardTests(unittest.TestCase):
    def test_share_card_text_extracts_json_payload(self):
        from bot.dispatcher import _share_card_text
        message = [
            {"type": "json", "data": {"data":
                "{\"app\":\"com.tencent.structmsg\",\"meta\":{\"detail_1\":{\"qqdocurl\":\"https:\\/\\/b23.tv\\/abc123\"}}}"}},
        ]
        text = _share_card_text(message)
        self.assertIn("https://b23.tv/abc123", text)

    def test_share_card_text_ignores_other_segments(self):
        from bot.dispatcher import _share_card_text
        self.assertEqual(_share_card_text([{"type": "text", "data": {"text": "hi"}}]), "")
        self.assertEqual(_share_card_text(None), "")
        self.assertEqual(_share_card_text([{"type": "json", "data": {}}]), "")


class HotboardFormatTests(unittest.TestCase):
    def test_format_includes_links_and_summary(self):
        from bot.scheduler import format_hotboard
        items = [{"title": "大新闻", "url": "https://example.com/1",
                  "hot_value": "123"},
                 {"title": "没链接的", "url": ""}]
        text = format_hotboard("weibo", items, summary="今天都在聊大新闻")
        lines = text.split("\n")
        self.assertEqual(lines[0], "【微博热榜】")
        self.assertEqual(lines[1], "今天都在聊大新闻")
        self.assertIn("https://example.com/1", text)
        self.assertIn("2. 没链接的", text)

    def test_format_without_summary(self):
        from bot.scheduler import format_hotboard
        text = format_hotboard("zhihu", [{"title": "题", "url": "u"}])
        self.assertTrue(text.startswith("【知乎热榜】\n1. 题"))

    def test_forward_nodes_split_header_and_items(self):
        from bot.scheduler import build_hotboard_forward_nodes
        nodes = build_hotboard_forward_nodes(
            "weibo",
            [
                {"title": "大新闻", "url": "https://example.com/1",
                 "hot_value": "123"},
                {"title": "第二条", "url": "", "hot_value": ""},
            ],
            bot_qq=222,
            summary="今天都在聊大新闻",
        )
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]["data"]["content"], '【微博热榜】\n今天都在聊大新闻')
        self.assertEqual(nodes[0]["data"]["uin"], "222")
        self.assertIn("1. 大新闻（123）", nodes[1]["data"]["content"])
        self.assertIn("参考来源：\nhttps://example.com/1", nodes[1]["data"]["content"])
        self.assertNotIn("?", nodes[1]["data"]["content"])
        self.assertIn("https://example.com/1", nodes[1]["data"]["content"])
        self.assertEqual(nodes[2]["data"]["content"], "2. 第二条")

    def test_forward_nodes_use_readable_empty_title(self):
        from bot.scheduler import build_hotboard_forward_nodes
        nodes = build_hotboard_forward_nodes(
            "weibo", [{"title": "", "url": ""}], bot_qq=222)
        self.assertEqual(nodes[1]["data"]["content"], "1. 暂无标题")


class HotboardPushTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_push_uses_merged_forward(self):
        from unittest.mock import AsyncMock
        from bot import scheduler
        sent = []

        class Client:
            async def send_group_forward_msg(self, group_id, nodes):
                sent.append((group_id, nodes))
                return {"status": "ok", "retcode": 0}

            async def send_group_msg(self, group_id, message):
                raise AssertionError("hotboard push must use merged forward")

        class Stub:
            config = {
                "bot_qq": 222,
                "uapi_api_key": "test",
                "hotboard_push": {"enabled": True, "types": ["weibo"]},
                "groups": {"100": {"enabled": True, "features": {}}},
            }
            client = Client()

        async def fake_uapi_get(dispatcher, path, params=None, kind="auto"):
            return {"list": [
                {"title": "热点一", "url": "https://example.com/1",
                 "hot_value": "999"},
                {"title": "热点二", "url": "https://example.com/2"},
            ]}

        async def fake_digest(dispatcher, board, items):
            return {"summary": '热点概况', "details": ['细节一', '细节二'],
                    "items": items}

        with patch("bot.uapi.credits_available", return_value=True), \
                patch("bot.uapi.uapi_get", fake_uapi_get), \
                patch.object(scheduler, "build_detailed_hotboard", fake_digest), \
                patch("bot.scheduler.asyncio.sleep", new=AsyncMock()):
            await scheduler._daily_hotboard_push(Stub())

        self.assertEqual(len(sent), 1)
        group_id, nodes = sent[0]
        self.assertEqual(group_id, 100)
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]["data"]["content"], '【微博热榜】\n热点概况')
        self.assertIn("热点一（999）", nodes[1]["data"]["content"])


class HotboardDigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_digest_uses_enriched_evidence(self):
        from bot.services import hotboard_digest
        class Client:
            session = object()
        class Stub:
            config = {}
            client = Client()
        enriched = [{"title": "topic", "hot_value": "1", "url": "https://example.com",
                     "evidence": "verified details", "sources": ["https://example.com"]}]
        response = json.dumps({"overview": "overview", "items": ["detail"]})
        with patch.object(hotboard_digest, "enrich_hotboard_items", new=AsyncMock(return_value=enriched)), \
                patch("bot.ai._call_deepseek", new=AsyncMock(return_value=response)):
            result = await hotboard_digest.build_hotboard_digest(Stub(), "weibo", "board", enriched)
        self.assertEqual(result["summary"], "overview")
        self.assertEqual(result["details"], ["detail"])
        self.assertEqual(result["items"][0]["evidence"], "verified details")

    def test_fallback_digest_summarizes_search_evidence(self):
        from bot.services import hotboard_digest

        title = "笔试第一称被第二名花钱劝弃考"
        duplicate = (
            "广东一事业单位笔试第一考生被第二名花钱劝弃考："
            "1 天前 · 广东陈女士称面试前收到劝弃考传话，教育局已开展核查。"
        )
        items = [{
            "title": title,
            "evidence": duplicate + "\n" + duplicate,
            "hot_value": "1481781",
        }]
        overview, details = hotboard_digest._fallback_digest("微博", items)

        self.assertIn("教育考试", overview)
        self.assertIn("热度靠前", overview)
        self.assertEqual(details[0].count("教育局已开展核查"), 1)
        self.assertNotIn("1 天前", details[0])
        self.assertNotIn(title + "：", details[0])
        self.assertLessEqual(len(details[0]), 170)

    def test_fallback_digest_removes_embedded_relative_time_labels(self):
        from bot.services import hotboard_digest

        title = "笔试第一称被第二名花钱劝弃考"
        items = [{
            "title": title,
            "evidence": (
                title + "?1 天前 · 广东陈女士称面试前收到劝弃考传话，教育局已开展核查。"
                + title + "?2 小时之前 · 广东陈女士称面试前收到劝弃考传话，教育局已开展核查。"
            ),
            "hot_value": "1481781",
        }]

        _, details = hotboard_digest._fallback_digest("微博", items)

        self.assertNotIn("1 天前", details[0])
        self.assertNotIn("2 小时之前", details[0])
        self.assertEqual(details[0].count("教育局已开展核查"), 1)

    def test_stale_search_result_is_rejected(self):
        from bot.services import hotboard_digest

        now = datetime(2026, 8, 5)
        self.assertTrue(hotboard_digest._search_result_is_stale({
            "title": "旧报道", "snippet": "2024年11月21日 · 旧案件回顾",
        }, now=now))
        self.assertFalse(hotboard_digest._search_result_is_stale({
            "title": "最新进展", "snippet": "2026年7月31日 · 相关部门回应",
        }, now=now))

    async def test_partial_ai_digest_keeps_valid_items_and_fills_fallback(self):
        from bot.services import hotboard_digest

        class Client:
            session = object()

        class Stub:
            config = {}
            client = Client()

        enriched = [
            {"title": "热点一", "hot_value": "1", "url": "https://example.com/1",
             "evidence": "部门已经发布正式回应。", "sources": ["https://example.com/1"]},
            {"title": "热点二", "hot_value": "2", "url": "https://example.com/2",
             "evidence": "相关调整将于本周正式执行。", "sources": ["https://example.com/2"]},
        ]
        response = "概括如下：\n```json\n" + json.dumps({
            "overview": "今天主要是两项公共事务的新进展。",
            "items": [{"index": 1, "summary": "第一项已有部门正式回应。"}],
        }, ensure_ascii=False) + "\n```"
        with patch.object(hotboard_digest, "enrich_hotboard_items",
                          new=AsyncMock(return_value=enriched)), \
                patch("bot.ai._call_deepseek", new=AsyncMock(return_value=response)):
            result = await hotboard_digest.build_hotboard_digest(
                Stub(), "weibo", "微博", enriched)

        self.assertEqual(result["summary"], "今天主要是两项公共事务的新进展。")
        self.assertEqual(result["details"][0], "第一项已有部门正式回应。")
        self.assertIn("本周正式执行", result["details"][1])


class AcgPushTests(unittest.IsolatedAsyncioTestCase):
    def _stub(self, client):
        class Stub:
            config = {
                "bot_qq": 222,
                "uapi_api_key": "test",
                "acg_images": {"enabled": True, "send_count": 20, "dedupe_days": 7},
                "groups": {"100": {"enabled": True, "features": {}}},
            }
        stub = Stub()
        stub.client = client
        return stub

    def test_provider_switch_clears_legacy_images_but_keeps_pending_due(self):
        state = scheduler._new_acg_state()
        state.update({
            "provider": "legacy",
            "recent": {"https://legacy.example/recent.jpg": time.time()},
            "pool": ["https://legacy.example/pool.jpg"],
            "pending_due": True,
            "delivery": {"urls": ["https://legacy.example/delivery.jpg"]},
            "last_failure": {"reason": "legacy_failure"},
        })
        stub = self._stub(type("Client", (), {})())

        self.assertTrue(scheduler._ensure_acg_provider(state, stub))
        self.assertEqual(state["provider"], "mukyu")
        self.assertEqual(state["recent"], {})
        self.assertEqual(state["pool"], [])
        self.assertTrue(state["pending_due"])
        self.assertIsNone(state["delivery"])
        self.assertIsNone(state["last_failure"])

    async def test_does_not_send_below_twenty(self):
        from bot import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "acg.json")
            sent = []
            class Client:
                async def send_group_forward_msg(self, group_id, nodes):
                    sent.append(nodes)
                    return {"status": "ok"}
            stub = self._stub(Client())
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path):
                state = scheduler._new_acg_state()
                state["pool"] = ["u{}".format(index) for index in range(19)]
                state["pending_due"] = True
                scheduler._save_acg_state(state)
                await scheduler._try_send_acg_delivery(stub)
            self.assertEqual(sent, [])

    async def test_sends_exactly_twenty_and_keeps_surplus(self):
        from bot import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "acg.json")
            sent = []
            class Client:
                async def send_group_forward_msg(self, group_id, nodes):
                    sent.append((group_id, nodes))
                    return {"status": "ok"}
            stub = self._stub(Client())
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path), \
                    patch("bot.scheduler.asyncio.sleep", new=AsyncMock()):
                state = scheduler._new_acg_state()
                state["pool"] = ["u{}".format(index) for index in range(25)]
                state["pending_due"] = True
                scheduler._save_acg_state(state)
                await scheduler._try_send_acg_delivery(stub)
                state = scheduler._load_acg_state()
            self.assertEqual(len(sent), 2)
            for call_index, (group_id, nodes) in enumerate(sent):
                image_nodes = [node for node in nodes
                               if isinstance(node["data"]["content"], list)]
                self.assertEqual(len(image_nodes), 10)
                header = nodes[0]["data"]["content"]
                self.assertRegex(
                    header, r"^小汐的每日图片 · 批次 #\d{4}-[0-9a-f]{6}-%d · 共10张$"
                    % (call_index + 1))
                self.assertNotIn("?", header)
            self.assertEqual(len(state["pool"]), 5)
            self.assertFalse(state["pending_due"])
            self.assertIsNone(state["delivery"])

    async def test_seven_day_dedupe_allows_expired_url(self):
        from bot import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "acg.json")
            class Client:
                is_connected = True
            stub = self._stub(Client())
            async def same_image(dispatcher, **kwargs):
                return MukyuImage(
                    url="u1", image_id=1, x_restrict=0, width=1, height=1,
                    extension="jpg", ai_type=0, illust_type=0)
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path), \
                    patch("bot.integrations.mukyu.fetch_random_image", same_image):
                state = scheduler._new_acg_state()
                state["recent"] = {"u1": time.time()}
                scheduler._save_acg_state(state)
                self.assertFalse(await scheduler._collect_one_acg_image(stub))
                state["recent"] = {"u1": time.time() - 8 * 86400}
                scheduler._save_acg_state(state)
                self.assertTrue(await scheduler._collect_one_acg_image(stub))
                self.assertEqual(scheduler._load_acg_state()["pool"], ["u1"])

    async def test_failed_delivery_is_persisted_for_retry(self):
        from bot import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "acg.json")
            class Client:
                async def send_group_forward_msg(self, group_id, nodes):
                    return {"status": "failed"}
            stub = self._stub(Client())
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path), \
                    patch("bot.scheduler.asyncio.sleep", new=AsyncMock()):
                state = scheduler._new_acg_state()
                state["pool"] = ["u{}".format(index) for index in range(20)]
                state["pending_due"] = True
                scheduler._save_acg_state(state)
                await scheduler._try_send_acg_delivery(stub)
                state = scheduler._load_acg_state()
            self.assertEqual(state["delivery"]["remaining_groups"], ["100"])
            self.assertGreater(state["delivery"]["next_retry_at"], time.time())


class DelayedQueueWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_does_not_spin_when_event_left_set(self):
        # Regression: enqueue left _delayed_queue_event set; the worker's
        # non-empty branch waited on the set event, which returns instantly,
        # spinning the loop and leaking ~65K TimerHandles/s until OOM.
        import heapq, time as _time
        from bot.dispatcher import Dispatcher
        d = Dispatcher.__new__(Dispatcher)
        entry = [_time.time() + 300, 1, 2, 0, [], "hi", "n"]
        d._delayed_queue = [entry]
        d._delayed_queue_index = {(1, 2): entry}
        d._delayed_queue_event = asyncio.Event()
        d._delayed_queue_event.set()  # stale set state that caused the spin

        calls = 0
        real_wait_for = asyncio.wait_for

        async def counting_wait_for(aw, timeout=None):
            nonlocal calls
            calls += 1
            return await real_wait_for(aw, timeout=timeout)

        with patch("bot.dispatcher.asyncio.wait_for", counting_wait_for):
            task = asyncio.create_task(d._delayed_queue_worker())
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertLessEqual(calls, 3)

    async def test_worker_fires_mature_entry(self):
        import time as _time
        from bot.dispatcher import Dispatcher
        d = Dispatcher.__new__(Dispatcher)
        entry = [_time.time() - 1, 1, 2, 0, [], "hi", "n"]
        d._delayed_queue = [entry]
        d._delayed_queue_index = {(1, 2): entry}
        d._delayed_queue_event = asyncio.Event()
        fired = []
        d.create_background_task = lambda coro, name="": (fired.append(coro), coro.close())[0]
        d._delayed_worker_task = None
        task = asyncio.create_task(d._delayed_queue_worker())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertEqual(len(fired), 1)
        self.assertEqual(d._delayed_queue_index, {})


class AIToolRegistryTests(unittest.TestCase):
    """Three-tier tool registry: completeness, scene gating, schema validity."""

    EXPECTED_READ = {
        "get_group_info", "get_member_info", "get_recent_messages",
        "get_group_files", "get_file_url", "get_group_notice",
        "get_group_honor", "get_shut_list", "get_friend_info", "ocr_image",
        "get_essence_list", "get_group_info_ex", "check_url_safely",
        "translate_en2zh", "get_group_at_all_remain", "uapi_weather",
        "uapi_hotboard", "uapi_saying", "uapi_answerbook", "uapi_epic_free",
        "get_group_msg_history", "get_forward_msg", "get_friend_list",
        "get_recent_contact", "uapi_search", "uapi_translate",
    }

    def test_three_tiers_complete(self):
        import ai_tools
        tiers = {}
        for name, entry in ai_tools.TOOL_REGISTRY.items():
            tiers.setdefault(entry["tier"], set()).add(name)
        self.assertEqual(tiers.get("read"), self.EXPECTED_READ)
        self.assertEqual(tiers.get("interaction"),
                         {"set_msg_emoji_like", "send_like", "send_music_card"})
        self.assertEqual(tiers.get("playful"), {"playful_ban"})

    def test_dangerous_tools_in_no_tier(self):
        import ai_tools
        for name in ("kick_member", "ban_member", "unban_member", "whole_ban",
                     "set_group_kick", "set_group_ban", "set_group_whole_ban",
                     "kick", "ban", "unban", "allban"):
            self.assertNotIn(name, ai_tools.TOOL_REGISTRY)

    def test_every_tool_has_valid_schema(self):
        import ai_tools
        for name, entry in ai_tools.TOOL_REGISTRY.items():
            params = entry["parameters"]
            self.assertEqual(params.get("type"), "object", name)
            self.assertIsInstance(params.get("properties"), dict, name)
            self.assertIsInstance(params.get("required"), list, name)
            for req in params["required"]:
                self.assertIn(req, params["properties"], name)
            self.assertTrue(entry.get("description"), name)
            self.assertTrue(callable(entry.get("handler")), name)

    def test_scene_gating(self):
        import ai_tools
        read_only = ai_tools.build_tool_schemas(explicit=False)
        names = {t["function"]["name"] for t in read_only}
        self.assertIn("get_group_info", names)
        self.assertNotIn("send_like", names)
        self.assertNotIn("send_music_card", names)
        self.assertNotIn("playful_ban", names)
        full = ai_tools.build_tool_schemas(explicit=True)
        full_names = {t["function"]["name"] for t in full}
        self.assertEqual(full_names, set(ai_tools.TOOL_REGISTRY))
        for t in full:
            self.assertEqual(t.get("type"), "function")
            fn = t["function"]
            self.assertTrue(fn.get("name"))
            self.assertTrue(fn.get("description"))
            self.assertEqual(fn["parameters"].get("type"), "object")

    def test_context_group_id_is_filtered_for_plain_uapi_tool(self):
        import ai_tools

        class Stub:
            config = {}

        with patch("bot.uapi.uapi_get", new=AsyncMock(return_value={"weather": "晴"})) as get:
            result = asyncio.run(ai_tools.execute_ai_tool(
                Stub(), "uapi_weather", {"city": "杭州"}, group_id=123,
            ))
        self.assertTrue(result["ok"])
        get.assert_awaited_once()
        self.assertEqual(get.await_args.kwargs["params"], {"city": "杭州"})


class PlayfulBanTests(unittest.IsolatedAsyncioTestCase):
    """playful_ban hard constraints: clamp, quota, target protection, cooldown."""

    GROUP = 100
    BOT = 222

    def _dispatcher(self, roles=None, bot_role="owner", masters=None):
        from bot import permission
        permission._bot_role_cache.clear()
        roles = roles or {}
        bot_qq = self.BOT
        bans = []

        class Client:
            session = None

            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                if user_id == bot_qq:
                    return {"status": "ok", "data": {"role": bot_role}}
                return {"status": "ok", "data": {"role": roles.get(user_id, "member")}}

            async def set_group_ban(self, group_id, user_id, duration=1800):
                bans.append((group_id, user_id, duration))
                return {"status": "ok"}

        class Stub:
            config = {
                "bot_owner": 111,
                "bot_qq": 222,
                "group_defaults": {},
                "groups": {"100": {"enabled": True,
                                   "masters": masters if masters is not None else []}},
            }
            client = Client()

        return Stub(), bans

    def _ai_tools(self, tmp):
        import ai_tools
        ai_tools.reset_playful_ban_for_test()
        ai_tools._PLAYFUL_BAN_AUDIT = os.path.join(tmp, "audit.json")
        return ai_tools

    def _ctx(self, target=666):
        return {"group_id": self.GROUP, "user_id": 333, "message_id": 0}

    async def test_duration_clamped_to_1_120(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher()
            await ai_tools.execute_playful_ban(
                stub, {"user_id": 666, "duration": 9999}, self._ctx())
            ai_tools._playful_ban_last_ts.clear()
            await ai_tools.execute_playful_ban(
                stub, {"user_id": 667, "duration": -5}, self._ctx())
            self.assertEqual([b[2] for b in bans], [120, 1])

    async def test_reason_truncated_to_50(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, _ = self._dispatcher()
            await ai_tools.execute_playful_ban(
                stub, {"user_id": 666, "reason": "哈" * 100}, self._ctx())
            audit = await asyncio.to_thread(
                lambda: _json.loads(
                    Path(ai_tools._PLAYFUL_BAN_AUDIT).read_text(encoding="utf-8")
                )
            )
            self.assertEqual(len(audit), 1)
            self.assertEqual(len(audit[0]["reason"]), 50)
            self.assertEqual(audit[0]["actor"], "AI")

    async def test_daily_group_limit_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher()
            for i in range(5):
                ai_tools._playful_ban_last_ts.clear()
                r = await ai_tools.execute_playful_ban(
                    stub, {"user_id": 700 + i}, self._ctx())
                self.assertTrue(r["ok"], r)
            ai_tools._playful_ban_last_ts.clear()
            r = await ai_tools.execute_playful_ban(
                stub, {"user_id": 705}, self._ctx())
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "daily_limit_reached")
            self.assertEqual(len(bans), 5)

    async def test_same_target_once_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher()
            r = await ai_tools.execute_playful_ban(stub, {"user_id": 666}, self._ctx())
            self.assertTrue(r["ok"], r)
            ai_tools._playful_ban_last_ts.clear()
            r = await ai_tools.execute_playful_ban(stub, {"user_id": 666}, self._ctx())
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "target_already_banned_today")
            self.assertEqual(len(bans), 1)

    async def test_admin_level_targets_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher(
                roles={555: "admin", 444: "owner"}, masters=[333])
            for target in (555, 444, 333, 111, 222):  # admin/gowner/master/owner/bot
                ai_tools._playful_ban_last_ts.clear()
                r = await ai_tools.execute_playful_ban(
                    stub, {"user_id": target}, self._ctx())
                self.assertFalse(r["ok"], "target %s must be protected" % target)
                self.assertEqual(r["error"], "target_protected")
            self.assertEqual(bans, [])

    async def test_group_cooldown_60s(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher()
            r = await ai_tools.execute_playful_ban(stub, {"user_id": 666}, self._ctx())
            self.assertTrue(r["ok"], r)
            r = await ai_tools.execute_playful_ban(stub, {"user_id": 667}, self._ctx())
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "cooldown_active")
            self.assertEqual(len(bans), 1)

    async def test_bot_must_be_group_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher(bot_role="member")
            r = await ai_tools.execute_playful_ban(stub, {"user_id": 666}, self._ctx())
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "bot_not_admin")
            self.assertEqual(bans, [])

    async def test_private_chat_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_tools = self._ai_tools(tmp)
            stub, bans = self._dispatcher()
            r = await ai_tools.execute_playful_ban(
                stub, {"user_id": 666}, {"group_id": 0, "user_id": 333, "message_id": 0})
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "group_only")
            self.assertEqual(bans, [])


class ChatWithToolsTests(unittest.IsolatedAsyncioTestCase):
    """Multi-round native function-calling loop and degrade paths."""

    def _stub(self, config=None):
        class Client:
            session = None

        class Stub:
            pass

        stub = Stub()
        stub.config = config or {"runtime": {}, "sigmai_api_key": "x"}
        stub.client = Client()
        return stub

    async def test_tool_call_then_final_text(self):
        import ai_tools
        from bot import ai as ai_module
        from unittest.mock import AsyncMock
        executed = []

        async def fake_execute(dispatcher, name, args, **kw):
            executed.append((name, args))
            return {"ok": True, "tool": name, "data": {"weather": "晴"}}

        responses = [
            {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "uapi_weather",
                              "arguments": '{"city": "杭州"}'}}]},
            {"content": "杭州今天晴天"},
        ]
        seen_conversations = []

        async def fake_inner(config, messages, max_tokens=400, temperature=0.7,
                             session=None, tools=None):
            seen_conversations.append((list(messages), tools))
            return responses.pop(0)

        ai_module._PROVIDER_NO_TOOLS.clear()
        with patch.object(ai_module, "_call_deepseek_inner", new=fake_inner), \
             patch.object(ai_tools, "execute_ai_tool", new=fake_execute):
            reply = await ai_module._chat_with_tools(
                self._stub(), [{"role": "user", "content": "杭州天气咋样"}],
                ai_tools.build_tool_schemas(explicit=True), 100, 333)
        self.assertEqual(reply, "杭州今天晴天")
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0][0], "uapi_weather")
        self.assertEqual(executed[0][1].get("city"), "杭州")
        # tool result must be injected back before the second model call
        second_messages = seen_conversations[1][0]
        roles = [m.get("role") for m in second_messages]
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)

    async def test_rounds_exhausted_forces_plain_final_call(self):
        import ai_tools
        from bot import ai as ai_module
        calls = []

        async def fake_execute(dispatcher, name, args, **kw):
            return {"ok": True, "tool": name}

        async def fake_inner(config, messages, max_tokens=400, temperature=0.7,
                             session=None, tools=None):
            calls.append(tools)
            if tools:
                return {"content": None, "tool_calls": [
                    {"id": "c%d" % len(calls), "type": "function",
                     "function": {"name": "uapi_saying", "arguments": "{}"}}]}
            return "想不出别的了 就这样吧"

        ai_module._PROVIDER_NO_TOOLS.clear()
        with patch.object(ai_module, "_call_deepseek_inner", new=fake_inner), \
             patch.object(ai_tools, "execute_ai_tool", new=fake_execute):
            reply = await ai_module._chat_with_tools(
                self._stub(), [{"role": "user", "content": "随便聊聊"}],
                ai_tools.build_tool_schemas(explicit=True), 100, 333)
        self.assertEqual(reply, "想不出别的了 就这样吧")
        self.assertEqual(len(calls), 5)  # 4 tool rounds + 1 forced plain call
        self.assertIsNone(calls[-1])

    async def test_no_provider_keys_returns_none(self):
        from bot import ai as ai_module
        env = {"SIGMAI_API_KEY": "", "QQBOT_SIGMAI_API_KEY": "",
               "DEEPSEEK_API_KEY": "", "QQBOT_DEEPSEEK_API_KEY": ""}
        with patch.dict("os.environ", env):
            reply = await ai_module._chat_with_tools(
                self._stub(config={"runtime": {}}),
                [{"role": "user", "content": "hi"}],
                [{"type": "function", "function": {"name": "x"}}], 100, 333)
        self.assertIsNone(reply)

    async def test_tools_unsupported_provider_degrades(self):
        from bot import ai as ai_module
        sigmai_key = ("https://www.sigmai.net/v1", "DeepSeek-V4-Flash")
        deepseek_key = ("https://api.deepseek.com", "deepseek-chat")
        ai_module._PROVIDER_NO_TOOLS.update({sigmai_key, deepseek_key})
        try:
            self.assertFalse(ai_module._providers_support_tools(
                {"sigmai_api_key": "x", "deepseek_api_key": "y"}))
            reply = await ai_module._chat_with_tools(
                self._stub(), [{"role": "user", "content": "hi"}],
                [{"type": "function", "function": {"name": "x"}}], 100, 333)
            self.assertIsNone(reply)
        finally:
            ai_module._PROVIDER_NO_TOOLS.discard(sigmai_key)
            ai_module._PROVIDER_NO_TOOLS.discard(deepseek_key)

    async def test_provider_tools_rejection_recorded_on_400(self):
        from bot import ai as ai_module

        class Response:
            status = 400

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                return '{"error":{"message":"tools is not supported for this model"}}'

        class Session:
            def post(self, _url, **kwargs):
                return Response()

        config = {"deepseek_api_key": "test-key", "runtime": {}}
        key = ("https://api.deepseek.com", "deepseek-chat")
        ai_module._PROVIDER_NO_TOOLS.discard(key)
        tools = [{"type": "function", "function": {
            "name": "uapi_saying", "description": "x",
            "parameters": {"type": "object", "properties": {}, "required": []}}}]
        try:
            with patch.dict("os.environ", {"SIGMAI_API_KEY": "", "QQBOT_SIGMAI_API_KEY": ""}):
                reply = await ai_module._call_deepseek_inner(
                    config, [{"role": "user", "content": "hi"}],
                    session=Session(), tools=tools)
            self.assertIsNone(reply)
            self.assertIn(key, ai_module._PROVIDER_NO_TOOLS)
        finally:
            ai_module._PROVIDER_NO_TOOLS.discard(key)

    async def test_tools_param_reaches_payload(self):
        from bot import ai as ai_module
        captured = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class Session:
            def post(self, _url, **kwargs):
                captured.update(kwargs.get("json") or {})
                return Response()

        config = {"deepseek_api_key": "test-key", "runtime": {}}
        tools = [{"type": "function", "function": {
            "name": "uapi_saying", "description": "x",
            "parameters": {"type": "object", "properties": {}, "required": []}}}]
        with patch.dict("os.environ", {"SIGMAI_API_KEY": "", "QQBOT_SIGMAI_API_KEY": ""}):
            message = await ai_module._call_deepseek_inner(
                config, [{"role": "user", "content": "hi"}],
                session=Session(), tools=tools)
        self.assertEqual(captured.get("tools"), tools)
        self.assertEqual(captured.get("tool_choice"), "auto")
        self.assertEqual(message, {"content": "ok"})


class SearchWebFallbackTests(unittest.IsolatedAsyncioTestCase):
    """search_web: uapis aggregate search primary, Bing scrape fallback."""

    BING_HTML = ('<li class="b_algo"><h2><a href="http://example.com">必应标题</a></h2>'
                 '<p>必应抓取的摘要内容，足够长可以被解析出来。</p></li>')

    def _dispatcher(self, bing_html=""):
        html = bing_html

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                return html

        class Session:
            def __init__(self):
                self.get_calls = 0

            def get(self, url, **kwargs):
                self.get_calls += 1
                return Response()

        class Client:
            def __init__(self):
                self.session = Session()

        class Stub:
            pass

        d = Stub()
        d.config = {"web_search": {"enabled": True}, "uapi": {}}
        d.client = Client()
        d._search_sem = asyncio.Semaphore(1)
        d._web_search_cache = {}
        return d

    async def test_uapi_primary_and_bing_untouched(self):
        from unittest.mock import AsyncMock
        from bot import ai as ai_module
        from bot import uapi
        d = self._dispatcher(bing_html=self.BING_HTML)
        payload = {"results": [{"title": "聚合标题", "snippet": "聚合摘要",
                                "url": "http://uapi.example"}]}
        with patch.object(uapi, "credits_available", return_value=True), \
             patch.object(uapi, "uapi_post", new=AsyncMock(return_value=payload)):
            result = await ai_module.search_web(d, "今天有什么新闻")
        self.assertIn("聚合标题", result)
        self.assertIn("http://uapi.example", result)
        self.assertEqual(d.client.session.get_calls, 0, "Bing must not be hit when uapi works")

    async def test_bing_fallback_when_uapi_fails(self):
        from unittest.mock import AsyncMock
        from bot import ai as ai_module
        from bot import uapi
        d = self._dispatcher(bing_html=self.BING_HTML)
        with patch.object(uapi, "credits_available", return_value=True), \
             patch.object(uapi, "uapi_post", new=AsyncMock(return_value=None)):
            result = await ai_module.search_web(d, "今天有什么新闻")
        self.assertIn("必应标题", result)
        self.assertEqual(d.client.session.get_calls, 1)

    async def test_bing_fallback_when_budget_exhausted(self):
        from unittest.mock import AsyncMock
        from bot import ai as ai_module
        from bot import uapi
        d = self._dispatcher(bing_html=self.BING_HTML)
        with patch.object(uapi, "credits_available", return_value=False), \
             patch.object(uapi, "uapi_post", new=AsyncMock(return_value=None)) as post_mock:
            result = await ai_module.search_web(d, "今天有什么新闻")
        self.assertIn("必应标题", result)
        post_mock.assert_not_called()
        self.assertEqual(d.client.session.get_calls, 1)

    async def test_both_paths_fail_returns_empty_and_caches(self):
        from unittest.mock import AsyncMock
        from bot import ai as ai_module
        from bot import uapi
        d = self._dispatcher(bing_html="<html><body>nothing</body></html>")
        with patch.object(uapi, "credits_available", return_value=True), \
             patch.object(uapi, "uapi_post", new=AsyncMock(return_value=None)):
            result = await ai_module.search_web(d, "今天有什么新闻")
        self.assertEqual(result, "")
        self.assertIn("今天有什么新闻", d._web_search_cache)

    async def test_format_uapi_search_results_shapes(self):
        from bot.ai import _format_uapi_search_results
        # nested data.results shape
        text = _format_uapi_search_results(
            {"data": {"results": [{"title": "t1", "content": "c1"}]}})
        self.assertIn("t1", text)
        # bare list shape
        text = _format_uapi_search_results([{"name": "n1", "description": "d1"}])
        self.assertIn("n1", text)
        # unparseable payload -> empty (caller falls back to Bing)
        self.assertEqual(_format_uapi_search_results({"foo": "bar"}), "")
        self.assertEqual(_format_uapi_search_results(None), "")


class TouchGalBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def test_request_parser_handles_titles_and_platforms(self):
        from bot.touchgal import extract_resource_request, parse_command_query

        request = extract_resource_request("求《千恋万花》安卓直装资源")
        self.assertEqual(request["title"], "千恋万花")
        self.assertEqual(request["platform"], "android")
        self.assertTrue(request["strong"])
        self.assertEqual(parse_command_query("安卓 千恋万花")["title"], "千恋万花")
        self.assertEqual(parse_command_query("安卓 千恋万花")["platform"], "android")

    def test_request_parser_rejects_non_resource_chat(self):
        from bot.touchgal import extract_resource_request

        self.assertIsNone(extract_resource_request("有没有人今晚一起打游戏"))
        self.assertIsNone(extract_resource_request("资源"))

    def test_candidate_matching_uses_aliases(self):
        from bot.touchgal import select_candidate

        selected, ranked = select_candidate(
            "Saku Saku",
            [{"unique_id": "Abc12345", "name": "樱花樱花", "aliases": ["Saku Saku"]}],
        )
        self.assertEqual(selected["unique_id"], "Abc12345")
        self.assertEqual(ranked[0]["score"], 100)

    def test_safe_site_link_rejects_external_hosts(self):
        from bot.touchgal import _safe_site_link

        self.assertEqual(
            _safe_site_link("https://www.touchgal.ink/game/Abc12345", "https://www.touchgal.ink"),
            "https://www.touchgal.ink/game/Abc12345",
        )
        self.assertEqual(
            _safe_site_link("https://evil.example/game/Abc12345", "https://www.touchgal.ink"),
            "",
        )

    def test_settings_reject_unsafe_base_urls(self):
        from bot.touchgal import DEFAULT_API_BASE, DEFAULT_SITE_BASE, _settings

        class DispatcherStub:
            config = {
                "touchgal_api_base_url": "http://127.0.0.1:8080/api",
                "touchgal": {"site_base_url": "https://evil.example"},
            }

        settings = _settings(DispatcherStub())
        self.assertEqual(settings["api_base"], DEFAULT_API_BASE)
        self.assertEqual(settings["site_base"], DEFAULT_SITE_BASE)

    def test_settings_accept_https_api_override(self):
        from bot.touchgal import _settings

        class DispatcherStub:
            config = {"touchgal_api_base_url": "https://api.example.com/touchgal/"}

        self.assertEqual(
            _settings(DispatcherStub())["api_base"],
            "https://api.example.com/touchgal",
        )

    async def test_no_token_is_silent_for_auto_reply(self):
        from bot.touchgal import handle_auto_request, search_and_format

        class DispatcherStub:
            config = {"touchgal": {"enabled": True, "auto_reply": True}}

        result = await search_and_format(DispatcherStub(), "千恋万花", explicit=True)
        self.assertIn("Token", result["text"])
        self.assertFalse(await handle_auto_request(DispatcherStub(), 1, 2, "求千恋万花资源"))

    async def test_ambiguous_auto_request_does_not_interrupt_chat(self):
        from bot import touchgal

        class DispatcherStub:
            config = {"touchgal_api_token": "test", "touchgal": {"enabled": True}}

        with patch.object(touchgal, "search_games", new=AsyncMock(return_value={
            "ok": True,
            "items": [
                {"unique_id": "Abc12345", "name": "作品甲"},
                {"unique_id": "Def67890", "name": "作品乙"},
            ],
        })):
            result = await touchgal.search_and_format(
                DispatcherStub(), "作品", explicit=False,
            )
        self.assertFalse(result["handled"])
        self.assertEqual(result["text"], "")

class RuntimeConcurrencyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_discard_updates_byte_accounting(self):
        client = OneBotClient({
            "ws_url": "ws://127.0.0.1:3001",
            "token": "",
            "bot_qq": 1,
            "runtime": {},
        })
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(({"post_type": "message"}, 128))
        client._queue_bytes = 128
        self.assertTrue(client._discard_oldest_queued_event(queue))
        self.assertEqual(client._queue_bytes, 0)
        self.assertTrue(queue.empty())

    async def test_voice_quota_check_is_serialized(self):
        from bot.services import voice_reply

        class Client:
            def __init__(self):
                self.calls = 0
                self.active = 0
                self.max_active = 0

            async def send_group_ai_record(self, group_id, character, text):
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0)
                self.active -= 1
                return {"status": "ok", "retcode": 0}

        class Stub:
            config = {
                "voice_reply": {
                    "enabled": True,
                    "probability": 1.0,
                    "min_chars": 1,
                    "max_chars": 45,
                    "cooldown_seconds": 3600,
                    "daily_limit": 1,
                    "character_id": "voice",
                },
                "group_defaults": {"features": {"voice_reply": True}},
                "groups": {"100": {"enabled": True, "features": {}}},
            }

        stub = Stub()
        stub.client = Client()
        with tempfile.TemporaryDirectory() as directory,                 patch.object(voice_reply, "_STATE_PATH", os.path.join(directory, "voice.json")),                 patch.object(voice_reply.random, "random", return_value=0):
            voice_reply.reset_state_for_test()
            results = await asyncio.gather(
                voice_reply.maybe_send_short_voice(stub, 100, "hello"),
                voice_reply.maybe_send_short_voice(stub, 100, "hello"),
            )
        self.assertEqual(results.count(True), 1)
        self.assertEqual(stub.client.calls, 1)
        self.assertEqual(stub.client.max_active, 1)
        voice_reply.reset_state_for_test()

    async def test_uapi_requests_use_bounded_concurrency(self):
        from bot.integrations import uapi

        class Stub:
            config = {}
            client = type("Client", (), {"session": object()})()

        active = 0
        max_active = 0

        async def fake_request(*args, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return {"ok": True}

        stub = Stub()
        with patch.object(uapi, "_json_request_unlocked", new=fake_request):
            await asyncio.gather(
                uapi._json_request(stub, "GET", "/saying"),
                uapi._json_request(stub, "GET", "/saying"),
            )
        self.assertEqual(max_active, 2)

    async def test_failed_scheduled_job_is_not_marked_done(self):
        dispatcher = object()
        with patch.object(scheduler, "_execute_scheduled_job", new=AsyncMock(return_value=False)),                 patch.object(scheduler, "_mark_scheduled_job_done") as mark_done:
            self.assertFalse(await scheduler._run_due_scheduled_job(
                dispatcher, "hotboard", 2))
        mark_done.assert_not_called()

    async def test_successful_scheduled_job_is_marked_done(self):
        dispatcher = object()
        with patch.object(scheduler, "_execute_scheduled_job", new=AsyncMock(return_value=True)),                 patch.object(scheduler, "_mark_scheduled_job_done") as mark_done:
            self.assertTrue(await scheduler._run_due_scheduled_job(
                dispatcher, "hotboard", 2))
        mark_done.assert_called_once_with(dispatcher, "hotboard", 2)
