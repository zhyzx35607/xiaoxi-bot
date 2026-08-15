"""Regression tests for the batch-2 permission and command-parsing fixes."""

import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

OWNER = 111
BOT = 222
MASTER = 333
GOWNER = 444
ADMIN_A = 555
ADMIN_B = 556
MEMBER = 666
GROUP = 100


class _Client:
    def __init__(self, roles=None, bot_role="owner"):
        self.roles = roles or {}
        self.bot_role = bot_role
        self.admin_calls = []
        self.kick_calls = []
        self.ban_calls = []
        self.group_messages = []

    async def get_group_member_info(self, group_id, user_id, no_cache=False):
        if user_id == BOT:
            return {"status": "ok", "data": {"role": self.bot_role}}
        return {"status": "ok", "data": {"role": self.roles.get(user_id, "member")}}

    async def set_group_admin(self, group_id, user_id, enable):
        self.admin_calls.append((group_id, user_id, enable))
        return {"status": "ok"}

    async def set_group_kick(self, group_id, user_id, reject_add_request=False):
        self.kick_calls.append((group_id, user_id))
        return {"status": "ok"}

    async def set_group_ban(self, group_id, user_id, duration):
        self.ban_calls.append((group_id, user_id, duration))
        return {"status": "ok"}

    async def delete_msg(self, message_id):
        return {"status": "ok"}

    async def send_group_msg(self, group_id, message):
        self.group_messages.append((group_id, message))
        return {"status": "ok"}


def _extract_mentions(message):
    targets = []
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = seg.get("data", {}).get("qq")
                if qq and qq != "all":
                    targets.append(int(qq))
    return targets


class _Dispatcher:
    def __init__(self, roles=None, bot_role="owner", masters=None, extra_config=None):
        from bot import permission
        permission._bot_role_cache.clear()
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
        if extra_config:
            for key, value in extra_config.items():
                if key == "groups":
                    self.config["groups"].update(value)
                else:
                    self.config[key] = value
        self.client = _Client(roles, bot_role)
        self.replies = []
        self._daily_fortunes = {}
        self.save_runtime_state = Mock()

    def _extract_mentions(self, message):
        return _extract_mentions(message)

    async def _reply(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    def _last_reply_text(self):
        return self.replies[-1][0][2]


def _at_segments(*qqs):
    return [{"type": "at", "data": {"qq": str(q)}} for q in qqs]


class AdminMgrHierarchyTests(unittest.IsolatedAsyncioTestCase):
    """Fix 1: /admin add|del must enforce the same hierarchy as kick/ban."""

    async def _run(self, actor, target, action="del", roles=None):
        from bot.commands.admin import cmd_admin_mgr

        d = _Dispatcher(roles=roles or {})
        message = _at_segments(target)
        await cmd_admin_mgr(d, GROUP, actor, action, "member", "card", message)
        return d

    async def test_admin_cannot_del_peer_admin(self):
        d = await self._run(ADMIN_A, ADMIN_B, "del",
                            roles={ADMIN_A: "admin", ADMIN_B: "admin"})
        self.assertEqual([], d.client.admin_calls)
        self.assertIn("同级", d._last_reply_text())

    async def test_owner_account_is_protected(self):
        d = await self._run(MASTER, OWNER, "del", roles={})
        self.assertEqual([], d.client.admin_calls)
        self.assertIn("受保护", d._last_reply_text())

    async def test_bot_account_is_protected(self):
        d = await self._run(MASTER, BOT, "del", roles={})
        self.assertEqual([], d.client.admin_calls)
        self.assertIn("受保护", d._last_reply_text())

    async def test_super_can_del_admin(self):
        d = await self._run(OWNER, ADMIN_A, "del", roles={ADMIN_A: "admin"})
        self.assertEqual([(GROUP, ADMIN_A, False)], d.client.admin_calls)

    async def test_admin_can_add_plain_member(self):
        d = await self._run(ADMIN_A, MEMBER, "add", roles={ADMIN_A: "admin"})
        self.assertEqual([(GROUP, MEMBER, True)], d.client.admin_calls)

    async def test_cannot_operate_on_group_owner(self):
        d = await self._run(OWNER, GOWNER, "del", roles={GOWNER: "owner"})
        self.assertEqual([], d.client.admin_calls)
        self.assertIn("群主", d._last_reply_text())


class BanDurationParsingTests(unittest.IsolatedAsyncioTestCase):
    """Fix 2: a 5-digit QQ number must not be parsed as the ban duration."""

    async def test_typed_qq_and_duration(self):
        from bot.commands.moderation import cmd_ban

        d = _Dispatcher()
        await cmd_ban(d, GROUP, OWNER, "12345 30", "owner", "card", [])
        self.assertEqual([(GROUP, 12345, 30 * 60)], d.client.ban_calls)

    async def test_typed_qq_without_duration_uses_default(self):
        from bot.commands.moderation import cmd_ban

        d = _Dispatcher()
        await cmd_ban(d, GROUP, OWNER, "12345", "owner", "card", [])
        self.assertEqual([(GROUP, 12345, 30 * 60)], d.client.ban_calls)

    async def test_natural_language_args_keep_duration(self):
        # message.py passes "f"{duration} {target}"" for natural triggers.
        from bot.commands.moderation import cmd_ban

        d = _Dispatcher()
        message = _at_segments(12345)
        await cmd_ban(d, GROUP, OWNER, "45 12345", "owner", "card", message)
        self.assertEqual([(GROUP, 12345, 45 * 60)], d.client.ban_calls)

    async def test_at_target_with_duration(self):
        from bot.commands.moderation import cmd_ban

        d = _Dispatcher()
        message = _at_segments(123456)
        await cmd_ban(d, GROUP, OWNER, "[CQ:at,qq=123456] 60", "owner", "card", message)
        self.assertEqual([(GROUP, 123456, 60 * 60)], d.client.ban_calls)


class NaturalTriggerMultiAtTests(unittest.IsolatedAsyncioTestCase):
    """Fix 3: "踢 @a @b" must kick each target exactly once."""

    def _harness(self, dispatcher):
        from bot.events.message import GroupMessageMixin
        from bot.commands.moderation import cmd_ban, cmd_kick, cmd_unban

        handlers = {"kick": cmd_kick, "ban": cmd_ban, "unban": cmd_unban}

        class Harness(GroupMessageMixin):
            def __init__(self):
                self.config = dispatcher.config
                self.client = dispatcher.client
                self.replies = dispatcher.replies

            async def _reply(self, *args, **kwargs):
                self.replies.append((args, kwargs))

            def _extract_mentions(self, message):
                return _extract_mentions(message)

            def _check_at_bot(self, message):
                return False

            def _check_name_mention(self, raw):
                return False

            async def _is_directed_at_bot(self, message, raw_message=""):
                return False

            async def _run_command(self, cmd, args, group_id, user_id, role,
                                   sender_card, message, request_message_id=0):
                await handlers[cmd](self, group_id, user_id, args, role,
                                    sender_card, message)

        return Harness()

    def _event_message(self, text, *qqs):
        segments = [{"type": "text", "data": {"text": text}}]
        segments.extend(_at_segments(*qqs))
        raw = text + " " + " ".join("[CQ:at,qq={}]".format(q) for q in qqs)
        return raw, segments

    async def test_kick_multiple_ats_runs_once_per_target(self):
        d = _Dispatcher(extra_config={"groups": {str(GROUP): {
            "enabled": True, "masters": [MASTER],
            "features": {"music": False, "galgame_resource": False},
        }}})
        d.config["bilibili"] = {"parse_enabled": False}
        harness = self._harness(d)
        raw, message = self._event_message("踢 ", MEMBER, 667)
        await harness._handle_group_message(
            GROUP, OWNER, message, raw, {"role": "member"}, "member", "card", 1)
        self.assertEqual(
            sorted([(GROUP, MEMBER), (GROUP, 667)]),
            sorted(d.client.kick_calls),
        )

    async def test_ban_multiple_ats_runs_once_per_target(self):
        d = _Dispatcher(extra_config={"groups": {str(GROUP): {
            "enabled": True, "masters": [MASTER],
            "features": {"music": False, "galgame_resource": False},
        }}})
        d.config["bilibili"] = {"parse_enabled": False}
        harness = self._harness(d)
        raw, message = self._event_message("禁言 ", MEMBER, 667)
        await harness._handle_group_message(
            GROUP, OWNER, message, raw, {"role": "member"}, "member", "card", 1)
        self.assertEqual(2, len(d.client.ban_calls))
        self.assertEqual(
            sorted([MEMBER, 667]),
            sorted(call[1] for call in d.client.ban_calls),
        )
        for _, _, duration in d.client.ban_calls:
            self.assertEqual(30 * 60, duration)


class AgentConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """Fix 4: the model's needs_confirmation=False must not waive confirmation
    for plans containing native write tools or an execution_plan."""

    def test_plan_rule_flags_write_tools(self):
        from bot.agent.runtime import _plan_requires_confirmation

        self.assertTrue(_plan_requires_confirmation({
            "needs_confirmation": False,
            "tools": [{"name": "agent_create_goal", "arguments": {"title": "x"}}],
        }))
        self.assertTrue(_plan_requires_confirmation({
            "needs_confirmation": False,
            "tools": [],
            "execution_plan": {"title": "t", "steps": [{"title": "s"}]},
        }))
        self.assertFalse(_plan_requires_confirmation({
            "needs_confirmation": False,
            "tools": [{"name": "agent_list_goals", "arguments": {}}],
        }))
        self.assertFalse(_plan_requires_confirmation({
            "needs_confirmation": False, "tools": [],
        }))
        self.assertTrue(_plan_requires_confirmation(None))

    def _runtime_config(self):
        return {
            "bot_owner": OWNER,
            "bot_qq": BOT,
            "agent": {},
            "groups": {"300": {"agent": {"primary_router": True, "enabled": True}}},
        }

    async def _handle(self, plan):
        from bot.agent.runtime import AgentRuntime

        config = self._runtime_config()
        with tempfile.TemporaryDirectory() as root:
            runtime = AgentRuntime(config, root)
            planner = type("Planner", (), {})()
            planner.plan = AsyncMock(return_value=plan)
            runtime.planner = planner
            runtime.run_autonomous = AsyncMock(return_value=({"reply": ""}, []))
            client = type("Client", (), {})()
            client.session = None
            client.send_group_msg_with_at = AsyncMock()
            dispatcher = type("D", (), {"config": config, "client": client})()
            event = {
                "post_type": "message", "message_type": "group", "group_id": 300,
                "user_id": GOWNER, "message_id": 7, "time": 1000,
                "raw_message": "帮我建个长期目标",
                "sender": {"role": "owner", "nickname": "群主"},
            }
            with patch(
                    "bot.services.confirmations.create_agent_confirmation",
                    return_value="CODE1") as create:
                handled = await runtime.handle_event(dispatcher, event, explicit=True)
            return handled, create, runtime

    async def test_write_tool_plan_requires_confirmation_despite_model_veto(self):
        plan = {
            "intent": "goal", "reply": "好", "reason": "建目标",
            "needs_confirmation": False,
            "tools": [{"name": "agent_create_goal", "arguments": {"title": "x"}}],
            "task": None, "execution_plan": None, "reflection": None,
        }
        handled, create, runtime = await self._handle(plan)
        self.assertTrue(handled)
        create.assert_called_once()
        runtime.run_autonomous.assert_not_called()

    async def test_execution_plan_requires_confirmation_despite_model_veto(self):
        plan = {
            "intent": "multi", "reply": "好", "reason": "多步任务",
            "needs_confirmation": False, "tools": [],
            "execution_plan": {"title": "t", "success_criteria": "", "steps": [{"title": "s", "success_criteria": ""}]},
            "task": None, "reflection": None,
        }
        handled, create, runtime = await self._handle(plan)
        self.assertTrue(handled)
        create.assert_called_once()
        runtime.run_autonomous.assert_not_called()

    async def test_read_only_plan_without_model_flag_skips_confirmation(self):
        plan = {
            "intent": "chat", "reply": "", "reason": "",
            "needs_confirmation": False,
            "tools": [{"name": "agent_list_goals", "arguments": {}}],
            "task": None, "execution_plan": None, "reflection": None,
        }
        handled, create, runtime = await self._handle(plan)
        self.assertFalse(handled)
        create.assert_not_called()
        runtime.run_autonomous.assert_called_once()

    async def test_moderation_action_never_reaches_gateway_execution(self):
        from bot.agent.tools.gateway import AgentToolGateway
        from bot.agent.runtime import AgentRuntime

        with tempfile.TemporaryDirectory() as root:
            runtime = AgentRuntime({"bot_owner": OWNER}, root)
            event = runtime.build_event(
                {"user_id": OWNER, "message_type": "private", "raw_message": "x"})
            dispatcher = type("D", (), {"config": {}, "client": object()})()
            result = await AgentToolGateway(dispatcher).execute(
                event, "set_group_ban", group_id=1, user_id=2, duration=60)
        self.assertFalse(result["ok"])
        self.assertEqual("unknown_agent_tool", result["error"])


class ScopeGateTests(unittest.TestCase):
    """Fix 5 + Fix 8: message_sent.private uses target_id; disabled groups
    let group masters through only for /enable."""

    def _dispatcher(self, groups=None, private_chat=None):
        return type("D", (), {"config": {
            "bot_owner": OWNER,
            "bot_qq": BOT,
            "command_prefix": "/",
            "group_defaults": {},
            "groups": groups or {},
            "private_chat": private_chat or {},
        }})()

    def test_message_sent_private_uses_target_id(self):
        from bot.events.context import _event_scope_allowed

        d = self._dispatcher()
        event = {
            "post_type": "message_sent", "message_type": "private",
            "user_id": BOT, "target_id": OWNER,
        }
        self.assertTrue(_event_scope_allowed(d, event))
        event["target_id"] = 999999
        self.assertFalse(_event_scope_allowed(d, event))
        # No target_id: fall back to user_id (the bot itself, not the owner).
        event.pop("target_id")
        self.assertFalse(_event_scope_allowed(d, event))

    def test_normal_private_message_unchanged(self):
        from bot.events.context import _event_scope_allowed

        d = self._dispatcher()
        self.assertTrue(_event_scope_allowed(d, {
            "post_type": "message", "message_type": "private", "user_id": OWNER}))
        self.assertFalse(_event_scope_allowed(d, {
            "post_type": "message", "message_type": "private", "user_id": MEMBER}))
        d_allow = self._dispatcher(private_chat={"enabled": True})
        self.assertTrue(_event_scope_allowed(d_allow, {
            "post_type": "message", "message_type": "private", "user_id": MEMBER}))

    def _group_event(self, user_id, raw):
        return {
            "post_type": "message", "message_type": "group",
            "group_id": GROUP, "user_id": user_id, "raw_message": raw,
        }

    def test_disabled_group_allows_master_enable(self):
        from bot.events.context import _event_scope_allowed

        d = self._dispatcher(groups={str(GROUP): {"enabled": False, "masters": [MASTER]}})
        self.assertTrue(_event_scope_allowed(d, self._group_event(MASTER, "/enable")))

    def test_disabled_group_rejects_member_enable(self):
        from bot.events.context import _event_scope_allowed

        d = self._dispatcher(groups={str(GROUP): {"enabled": False, "masters": [MASTER]}})
        self.assertFalse(_event_scope_allowed(d, self._group_event(MEMBER, "/enable")))

    def test_disabled_group_rejects_master_non_enable(self):
        from bot.events.context import _event_scope_allowed

        d = self._dispatcher(groups={str(GROUP): {"enabled": False, "masters": [MASTER]}})
        self.assertFalse(_event_scope_allowed(d, self._group_event(MASTER, "大家好")))

    def test_disabled_group_owner_and_bot_paths_unchanged(self):
        from bot.events.context import _event_scope_allowed

        d = self._dispatcher(groups={str(GROUP): {"enabled": False, "masters": [MASTER]}})
        self.assertTrue(_event_scope_allowed(d, self._group_event(OWNER, "随便说点啥")))
        self.assertTrue(_event_scope_allowed(d, self._group_event(BOT, "/enable")))
        self.assertFalse(_event_scope_allowed(d, self._group_event(BOT, "其他消息")))


class NaturalTriggerBoundaryTests(unittest.TestCase):
    """Fix 6: CJK keywords accept trailing aspect particles; ASCII keywords
    and unrelated hanzi compounds stay strict."""

    def _trigger(self, text, *qqs):
        from bot.natural_triggers import check_natural_triggers

        raw = text + " " + " ".join("[CQ:at,qq={}]".format(q) for q in qqs)
        return check_natural_triggers(raw, _at_segments(*qqs))

    def test_readme_examples_match(self):
        self.assertEqual("kick", self._trigger("踢了", MEMBER)[0])
        self.assertEqual("kick", self._trigger_for("把 @ 踢了"))
        self.assertEqual("ban", self._trigger("禁言", MEMBER)[0])
        self.assertEqual("ban", self._trigger_for("把 @ 禁言了"))
        self.assertEqual("unban", self._trigger("解禁", MEMBER)[0])

    def _trigger_for(self, text_with_placeholder):
        from bot.natural_triggers import check_natural_triggers

        raw = text_with_placeholder.replace("@", "[CQ:at,qq={}]".format(MEMBER))
        result = check_natural_triggers(raw, _at_segments(MEMBER))
        return result[0] if result else None

    def test_hanzi_compounds_do_not_match(self):
        self.assertIsNone(self._trigger("我们踢球去了", MEMBER))
        self.assertIsNone(self._trigger("踢踏舞真好看", MEMBER))

    def test_ascii_keywords_keep_strict_boundary(self):
        self.assertIsNone(self._trigger("banana真好吃", MEMBER))
        self.assertEqual("ban", self._trigger("ban", MEMBER)[0])


class GroupListCommandTests(unittest.IsolatedAsyncioTestCase):
    """Fix 7: /group list returns the configured group list."""

    async def test_group_list(self):
        from bot.commands.admin import cmd_group

        d = _Dispatcher(extra_config={"groups": {
            "111": {"enabled": True},
            "222": {"enabled": False},
        }})
        await cmd_group(d, None, OWNER, "list", "member", "", [])
        text = d._last_reply_text()
        self.assertIn("群组:", text)
        self.assertIn("111 [开启]", text)
        self.assertIn("222 [关闭]", text)

    async def test_group_list_empty(self):
        from bot.commands.admin import cmd_group

        d = _Dispatcher()
        d.config["groups"] = {}
        await cmd_group(d, None, OWNER, "list", "member", "", [])
        self.assertIn("还没有配置群", d._last_reply_text())


class BadWordWarningAtTests(unittest.IsolatedAsyncioTestCase):
    """Fix 9: the bad-word warning must mention the user with a real at segment."""

    async def test_warning_uses_at_segment(self):
        from bot.events.notice import check_bad_words

        d = _Dispatcher(extra_config={"groups": {str(GROUP): {
            "enabled": True,
            "bad_words": {
                "enabled": True, "auto_delete": False,
                "warn_msg": "@{user} 请注意文明发言！", "words": ["垃圾"],
            },
        }}})
        matched = await check_bad_words(d, GROUP, MEMBER, "你是垃圾", 42)
        self.assertTrue(matched)
        self.assertEqual(1, len(d.client.group_messages))
        _, message = d.client.group_messages[0]
        self.assertIsInstance(message, list)
        at_segments = [seg for seg in message if seg.get("type") == "at"]
        self.assertEqual([{"type": "at", "data": {"qq": str(MEMBER)}}], at_segments)
        text = "".join(seg["data"]["text"] for seg in message if seg.get("type") == "text")
        self.assertIn("请注意文明发言", text)
        self.assertNotIn(str(MEMBER), text)


class FortuneRetryTests(unittest.IsolatedAsyncioTestCase):
    """Fix 10: a failed fortune generation must not consume the daily attempt."""

    async def test_failure_allows_retry(self):
        from bot.commands.fun import cmd_fortune

        d = _Dispatcher()
        with patch("bot.ai.deepseek_chat",
                   new=AsyncMock(side_effect=["", "好运连连"])) as chat:
            await cmd_fortune(d, GROUP, MEMBER, "", "member", "小卡", [])
            self.assertEqual({}, d._daily_fortunes)
            d.save_runtime_state.assert_not_called()
            self.assertIn("等会再试", d._last_reply_text())

            await cmd_fortune(d, GROUP, MEMBER, "", "member", "小卡", [])
            self.assertEqual(1, len(d._daily_fortunes))
            self.assertIn("今日运势", d._last_reply_text())
            self.assertEqual(2, chat.await_count)


class HelpOwnerOnlyTests(unittest.TestCase):
    """Fix 11: help must hide/label owner_only commands like the permission check."""

    def test_owner_only_visibility(self):
        from bot.commands.system import _help_visible
        from bot.permission import LEVEL_GOWNER, LEVEL_MEMBER, LEVEL_SUPER

        info = {"owner_only": True}
        self.assertFalse(_help_visible(info, LEVEL_MEMBER, GROUP))
        self.assertTrue(_help_visible(info, LEVEL_GOWNER, GROUP))
        self.assertTrue(_help_visible(info, LEVEL_SUPER, GROUP))

    def test_owner_only_label(self):
        from bot.commands.system import _help_permission_label

        self.assertEqual("群主或最高主人", _help_permission_label({"owner_only": True}))
        self.assertEqual("所有人", _help_permission_label({}))


if __name__ == "__main__":
    unittest.main()
