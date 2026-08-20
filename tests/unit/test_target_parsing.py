"""Unified target parsing (@/QQ/multi/glued) and rich reply label tests."""

import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

OWNER = 111
BOT = 222
MASTER = 333
MEMBER = 666
GROUP = 100


def _at_segments(*qqs):
    return [{"type": "at", "data": {"qq": str(q)}} for q in qqs]


def _extract_mentions(message):
    targets = []
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = seg.get("data", {}).get("qq")
                if qq and qq != "all":
                    targets.append(int(qq))
    return targets


class _Client:
    def __init__(self, roles=None):
        self.roles = roles or {}
        self.admin_calls = []
        self.kick_calls = []
        self.ban_calls = []
        self.like_calls = []

    async def get_group_member_info(self, group_id, user_id, no_cache=False):
        return {"status": "ok", "data": {
            "role": self.roles.get(user_id, "member"),
            "card": "名片{}".format(user_id), "nickname": "昵称{}".format(user_id),
        }}

    async def get_stranger_info(self, user_id, no_cache=False):
        return {"status": "ok", "data": {"nickname": "昵称{}".format(user_id)}}

    async def get_group_info(self, group_id):
        return {"status": "ok", "data": {"group_name": "测试群"}}

    async def set_group_admin(self, group_id, user_id, enable):
        self.admin_calls.append((group_id, user_id, enable))
        return {"status": "ok"}

    async def set_group_kick(self, group_id, user_id, reject_add_request=False):
        self.kick_calls.append((group_id, user_id))
        return {"status": "ok"}

    async def set_group_ban(self, group_id, user_id, duration):
        self.ban_calls.append((group_id, user_id, duration))
        return {"status": "ok"}

    async def send_like(self, user_id, times):
        self.like_calls.append((user_id, times))
        return {"status": "ok"}


class _Dispatcher:
    def __init__(self, roles=None, masters=None):
        from bot import permission
        permission._bot_role_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self._config_path = self._tmp.name + "/config.json"
        self.config = {
            "bot_owner": OWNER,
            "bot_qq": BOT,
            "command_prefix": "/",
            "group_defaults": {},
            "groups": {str(GROUP): {
                "enabled": True,
                "masters": [MASTER] if masters is None else masters,
            }},
        }
        self.client = _Client(roles)
        self.replies = []
        self._daily_likes = {}
        self._daily_fortunes = {}
        self.save_runtime_state = Mock()
        self.save_runtime_state_async = AsyncMock()

    def _extract_mentions(self, message):
        return _extract_mentions(message)

    async def _reply(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    def _last_reply_text(self):
        return self.replies[-1][0][2]

    def _all_reply_text(self):
        return "\n".join(str(r[0][2]) for r in self.replies)


class ParseTargetQqsTests(unittest.TestCase):
    def test_bare_numbers_multiple(self):
        from bot.commands.common import parse_target_qqs
        targets, rest = parse_target_qqs("12345 23456")
        self.assertEqual([12345, 23456], targets)
        self.assertEqual("", rest)

    def test_mentions_preferred_and_deduped(self):
        from bot.commands.common import parse_target_qqs
        targets, rest = parse_target_qqs("[CQ:at,qq=12345] 12345 23456", [12345])
        self.assertEqual([12345, 23456], targets)
        self.assertEqual("", rest)

    def test_cq_codes_without_mentions(self):
        from bot.commands.common import parse_target_qqs
        targets, _ = parse_target_qqs("add[CQ:at,qq=12345][CQ:at,qq=23456]", [])
        self.assertEqual([12345, 23456], targets)

    def test_duration_unit_is_not_target(self):
        from bot.commands.common import parse_target_qqs
        targets, rest = parse_target_qqs("12345 43200分钟", [])
        self.assertEqual([12345], targets)
        self.assertIn("43200", rest)

    def test_glued_action_and_target(self):
        from bot.commands.common import split_action_args, parse_target_qqs
        action, rest = split_action_args("add[CQ:at,qq=12345]", ("add", "del", "list"))
        self.assertEqual("add", action)
        targets, _ = parse_target_qqs(rest, [])
        self.assertEqual([12345], targets)

    def test_glued_action_bare_number(self):
        from bot.commands.common import split_action_args
        action, rest = split_action_args("add12345", ("add", "del", "list"))
        self.assertEqual("add", action)
        self.assertEqual("12345", rest)

    def test_unknown_action_returns_default(self):
        from bot.commands.common import split_action_args
        action, rest = split_action_args("xyz 12345", ("add", "del"), default="list")
        self.assertEqual("list", action)
        self.assertEqual("xyz 12345", rest)


class LabelFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_label_uses_group_card(self):
        from bot.commands.common import format_user_label
        d = _Dispatcher()
        label = await format_user_label(d, GROUP, 12345)
        self.assertEqual("名片12345(12345)", label)

    async def test_user_label_falls_back_to_bare_qq(self):
        from bot.commands.common import format_user_label

        class _BrokenClient(_Client):
            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                return {"status": "failed"}

            async def get_stranger_info(self, user_id, no_cache=False):
                return {"status": "failed"}

        d = _Dispatcher()
        d.client = _BrokenClient()
        self.assertEqual("12345", await format_user_label(d, GROUP, 12345))

    async def test_group_label(self):
        from bot.commands.common import format_group_label
        d = _Dispatcher()
        self.assertEqual("测试群(100)", await format_group_label(d, GROUP))


class MasterCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_by_at_with_rich_reply(self):
        from bot.commands.admin import cmd_master
        d = _Dispatcher()
        await cmd_master(d, GROUP, OWNER, "add", "member", "card", _at_segments(12345))
        self.assertIn(12345, d.config["groups"][str(GROUP)]["masters"])
        text = d._last_reply_text()
        self.assertIn("测试群(100)", text)
        self.assertIn("名片12345(12345)", text)

    async def test_add_multiple_bare_qq(self):
        from bot.commands.admin import cmd_master
        d = _Dispatcher()
        await cmd_master(d, GROUP, OWNER, "add 12345 23456", "member", "card", [])
        masters = d.config["groups"][str(GROUP)]["masters"]
        self.assertIn(12345, masters)
        self.assertIn(23456, masters)
        self.assertIn("多了 2 个", d._last_reply_text())

    async def test_add_glued_at(self):
        from bot.commands.admin import cmd_master
        d = _Dispatcher()
        await cmd_master(d, GROUP, OWNER, "add[CQ:at,qq=12345]", "member", "card",
                         _at_segments(12345))
        self.assertIn(12345, d.config["groups"][str(GROUP)]["masters"])

    async def test_del_reports_absent_and_removed(self):
        from bot.commands.admin import cmd_master
        d = _Dispatcher(masters=[MASTER, 12345])
        await cmd_master(d, GROUP, OWNER, "del 12345 99999", "member", "card", [])
        masters = d.config["groups"][str(GROUP)]["masters"]
        self.assertNotIn(12345, masters)
        text = d._last_reply_text()
        self.assertIn("移除了", text)
        self.assertIn("本来就不是主人", text)

    async def test_list_shows_labels(self):
        from bot.commands.admin import cmd_master
        d = _Dispatcher(masters=[12345])
        await cmd_master(d, GROUP, OWNER, "list", "member", "card", [])
        self.assertIn("名片12345(12345)", d._last_reply_text())

    async def test_private_add_with_group_number(self):
        from bot.commands.admin import cmd_master
        d = _Dispatcher()
        d.config["groups"]["234567"] = {"enabled": True, "masters": []}

        class _Client2(_Client):
            async def get_group_info(self, group_id):
                return {"status": "ok", "data": {"group_name": "二群"}}

        d.client = _Client2()
        await cmd_master(d, None, OWNER, "add 234567 12345", "member", "card", [])
        self.assertIn(12345, d.config["groups"]["234567"]["masters"])
        self.assertIn("二群(234567)", d._last_reply_text())


class ModerationTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_kick_multiple_bare_qq(self):
        from bot.commands.moderation import cmd_kick
        d = _Dispatcher()
        await cmd_kick(d, GROUP, OWNER, "12345 23456", "owner", "card", [])
        self.assertEqual([(GROUP, 12345), (GROUP, 23456)], d.client.kick_calls)
        self.assertIn("名片12345(12345)", d._all_reply_text())

    async def test_ban_typed_qq_and_duration(self):
        from bot.commands.moderation import cmd_ban
        d = _Dispatcher()
        await cmd_ban(d, GROUP, OWNER, "12345 30", "owner", "card", [])
        self.assertEqual([(GROUP, 12345, 30 * 60)], d.client.ban_calls)

    async def test_ban_at_with_trailing_duration(self):
        from bot.commands.moderation import cmd_ban
        d = _Dispatcher()
        await cmd_ban(d, GROUP, OWNER, "[CQ:at,qq=123456] 43200", "owner", "card",
                      _at_segments(123456))
        self.assertEqual([(GROUP, 123456, 43200 * 60)], d.client.ban_calls)

    async def test_ban_multiple_targets_share_duration(self):
        from bot.commands.moderation import cmd_ban
        d = _Dispatcher()
        await cmd_ban(d, GROUP, OWNER, "12345 23456 10分钟", "owner", "card", [])
        self.assertEqual(
            [(GROUP, 12345, 600), (GROUP, 23456, 600)], d.client.ban_calls)

    async def test_ban_reply_contains_label(self):
        from bot.commands.moderation import cmd_ban
        d = _Dispatcher()
        await cmd_ban(d, GROUP, OWNER, "12345", "owner", "card", [])
        self.assertIn("名片12345(12345)", d._all_reply_text())

    async def test_admin_mgr_multiple_targets(self):
        from bot.commands.admin import cmd_admin_mgr
        d = _Dispatcher()
        await cmd_admin_mgr(d, GROUP, OWNER, "add 12345 23456", "member", "card", [])
        self.assertEqual(
            [(GROUP, 12345, True), (GROUP, 23456, True)], d.client.admin_calls)
        self.assertIn("名片12345(12345)", d._all_reply_text())


class LikeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_like_multiple_targets(self):
        from bot.commands.fun import cmd_like
        d = _Dispatcher()
        await cmd_like(d, GROUP, OWNER, "", "member", "card", _at_segments(12345, 23456))
        self.assertEqual([(12345, 10), (23456, 10)], d.client.like_calls)
        self.assertIn("名片12345(12345)", d._all_reply_text())

    async def test_like_defaults_to_self(self):
        from bot.commands.fun import cmd_like
        d = _Dispatcher()
        await cmd_like(d, GROUP, OWNER, "", "member", "card", [])
        self.assertEqual([(OWNER, 10)], d.client.like_calls)


if __name__ == "__main__":
    unittest.main()
