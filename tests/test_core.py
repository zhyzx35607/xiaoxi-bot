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
                for _ in range(15):
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

        jobs = dict(scheduler._scheduled_jobs(Stub()))
        self.assertIn("checkin", jobs)
        self.assertIn("acg", jobs)
        self.assertIn("hotboard", jobs)

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
        self._patcher.stop()
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


class AcgPushTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_roundtrip_and_forward_dedup(self):
        from bot import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path):
                scheduler._save_acg_history(["u1", "u2"])
                self.assertEqual(scheduler._load_acg_history(), ["u1", "u2"])

                sent = []

                class Client:
                    _running = True

                    async def send_group_forward_msg(self, group_id, nodes):
                        sent.append((group_id, nodes))
                        return {"status": "ok", "retcode": 0}

                class Stub:
                    config = {
                        "bot_qq": 222,
                        "acg_images": {"enabled": True, "count": 3},
                        "groups": {"100": {"enabled": True, "features": {}}},
                    }
                    client = Client()

                async def fake_resolve(dispatcher, path, params=None):
                    fake_resolve.n += 1
                    # u1 already in history: must be skipped
                    return ["u1", "u3", "u4", "u5"][fake_resolve.n % 4]
                fake_resolve.n = -1

                with patch("bot.uapi.uapi_resolve_image_url", fake_resolve):
                    await scheduler._daily_acg_push(Stub())
                self.assertEqual(len(sent), 1)
                gid, nodes = sent[0]
                self.assertEqual(gid, 100)
                urls = [n["data"]["content"][0]["data"]["file"] for n in nodes]
                self.assertNotIn("u1", urls)
                self.assertEqual(len(nodes), 3)
                self.assertIn("u3", scheduler._load_acg_history())


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
            with open(ai_tools._PLAYFUL_BAN_AUDIT, encoding="utf-8") as f:
                audit = _json.load(f)
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
