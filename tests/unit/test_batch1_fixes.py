"""批次一回归测试：AI 输出命令前缀剥离、转发降级链、按身份 prompt、get_bot_help、记忆容错。"""

import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.permission import LEVEL_ADMIN, LEVEL_MEMBER, LEVEL_SUPER
from bot.transport import output as output_module
from bot.transport.output import build_forward_nodes, send_text_response


class _OutputClient:
    def __init__(self, forward_id=7788):
        self.forward_id = forward_id
        self.group_messages = []
        self.private_messages = []
        self.forwards = []
        self.session = None

    async def send_group_msg(self, group_id, message):
        self.group_messages.append((group_id, message))
        return {"status": "ok", "data": {"message_id": 1}}

    async def send_private_msg(self, user_id, message):
        self.private_messages.append((user_id, message))
        return {"status": "ok", "data": {"message_id": 1}}

    async def send_group_forward_msg(self, group_id, nodes):
        self.forwards.append((group_id, nodes))
        data = {"message_id": self.forward_id} if self.forward_id else {}
        return {"status": "ok", "data": data}

    async def send_forward_msg(self, **kwargs):
        return {"status": "failed", "retcode": 1200}


def _output_dispatcher(client):
    config = {
        "bot_owner": 1, "bot_qq": 2,
        "message_output": {"forward_threshold_chars": 200,
                           "ai_summary_enabled": False},
    }
    return type("Stub", (), {"config": config, "client": client})()


class StripCommandPrefixTests(unittest.TestCase):
    def test_unit_variants(self):
        from bot.ai.reply import strip_command_prefix

        self.assertEqual(strip_command_prefix("/master add 123"), "／master add 123")
        # 行首判断：URL、分数、行中的斜杠不受影响
        self.assertEqual(strip_command_prefix("看 https://a.b/c 1/2 吧"),
                         "看 https://a.b/c 1/2 吧")
        # 空路径和注释式双斜杠不是命令
        self.assertEqual(strip_command_prefix("/"), "/")
        self.assertEqual(strip_command_prefix("/ 加空格"), "/ 加空格")
        self.assertEqual(strip_command_prefix("// 注释"), "// 注释")
        # 多行逐行处理
        self.assertEqual(strip_command_prefix("好啊\n/kick @某人\n再说"),
                         "好啊\n／kick @某人\n再说")
        self.assertEqual(strip_command_prefix(""), "")
        self.assertIsNone(strip_command_prefix(None))

    def test_post_process_reply_strips_prefix(self):
        from bot.ai.runtime import _post_process_reply

        self.assertTrue(_post_process_reply("/master add 123").startswith("／master"))

    def test_post_process_roleplay_reply_strips_prefix(self):
        from bot.ai.runtime import _post_process_roleplay_reply

        result = _post_process_roleplay_reply("/ban @某人 10")
        self.assertTrue(result.startswith("／ban"))


class ForwardSummaryStripTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_prefix_stripped_before_send(self):
        client = _OutputClient()
        dispatcher = _output_dispatcher(client)
        with patch.object(output_module, "_summarize",
                          new=AsyncMock(return_value="/master add 123")):
            await send_text_response(
                dispatcher, 100, 1, "a" * 300, force_forward=True)
        guide = client.group_messages[-1][1]
        text = next(seg["data"]["text"] for seg in guide if seg["type"] == "text")
        self.assertIn("／master add 123", text)
        self.assertNotIn("\n/master", text)


class ForwardDegradationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_log_includes_wording(self):
        class FailingClient(_OutputClient):
            async def send_group_forward_msg(self, group_id, nodes):
                return {"status": "failed", "retcode": 1200,
                        "wording": "nodes too long"}

            async def send_group_msg(self, group_id, message):
                self.group_messages.append((group_id, message))
                return {"status": "failed", "retcode": 1200}

        dispatcher = _output_dispatcher(FailingClient())
        with self.assertLogs("qqbot", level="WARNING") as captured:
            await send_text_response(
                dispatcher, 100, 1, "a" * 300, force_forward=True)
        joined = "\n".join(captured.output)
        self.assertIn("retcode=1200", joined)
        self.assertIn("nodes too long", joined)

    async def test_forward_failure_degrades_to_plain_messages(self):
        class FailingForwardClient(_OutputClient):
            async def send_group_forward_msg(self, group_id, nodes):
                return {"status": "failed", "retcode": 1200}

        client = FailingForwardClient()
        dispatcher = _output_dispatcher(client)
        sections = ["x" * 300, "y" * 300, "z" * 300]
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await send_text_response(
                dispatcher, 100, 1, "ignored", force_forward=True,
                sections=sections)
        self.assertEqual(result.get("fallback"), "plain_messages")
        self.assertEqual(len(client.group_messages), 3)
        for _group, message in client.group_messages:
            self.assertLessEqual(len(str(message)), 400)

    async def test_plain_fallback_over_five_goes_to_text_file(self):
        class FailingForwardClient(_OutputClient):
            async def send_group_forward_msg(self, group_id, nodes):
                return {"status": "failed", "retcode": 1200}

            async def upload_group_file(self, group_id, path, name):
                return {"status": "ok"}

        client = FailingForwardClient()
        dispatcher = _output_dispatcher(client)
        sections = ["第{}段".format(i) + "x" * 380 for i in range(10)]
        with patch("asyncio.sleep", new=AsyncMock()):
            await send_text_response(
                dispatcher, 100, 1, "ignored", force_forward=True,
                sections=sections)
        # 超过 5 条上限，普通消息降级放弃，直接走 txt 文件兜底
        self.assertEqual(len(client.group_messages), 1)
        self.assertIn("文本文件", str(client.group_messages[0][1]))

    def test_forward_node_hard_cap(self):
        client = _OutputClient()
        dispatcher = _output_dispatcher(client)
        nodes = build_forward_nodes(dispatcher, "ignored", sections=["a" * 3000])
        for node in nodes[1:]:
            self.assertLessEqual(len(node["data"]["content"]), 1000)
        self.assertGreater(len(nodes), 2)

    def test_plain_chunk_merge_rules(self):
        self.assertEqual(output_module._plain_fallback_chunks([]), [])
        # 总量超过 5 条 * 400 字时放弃，留给 txt 兜底
        self.assertEqual(
            output_module._plain_fallback_chunks(["x" * 380] * 10), [])
        chunks = output_module._plain_fallback_chunks(["x" * 200, "y" * 200])
        # 200+换行+200=401 超过 400 上限，必须拆成两条
        self.assertEqual(len(chunks), 2)
        chunks = output_module._plain_fallback_chunks(["x" * 199, "y" * 200])
        self.assertEqual(len(chunks), 1)


class PromptIdentityTests(unittest.TestCase):
    def test_style_rules_split_keeps_full_text(self):
        from bot.ai.prompts import STYLE_RULES, STYLE_RULES_COMMON

        self.assertIn("你自己搜下呗", STYLE_RULES)
        self.assertNotIn("你自己搜下呗", STYLE_RULES_COMMON)
        # 组合后仍是完整规则（搞颜色/政治条款保留在公共尾部）
        self.assertTrue(STYLE_RULES.startswith(STYLE_RULES_COMMON))
        self.assertIn("搞颜色", STYLE_RULES)
        self.assertIn("政治", STYLE_RULES)

    def test_style_rules_for_level(self):
        from bot.ai.prompts import (
            STYLE_RULES, STYLE_RULES_COMMON, _style_rules_for_level,
        )

        self.assertIs(_style_rules_for_level(LEVEL_SUPER), STYLE_RULES_COMMON)
        self.assertIs(_style_rules_for_level(LEVEL_MEMBER), STYLE_RULES)

    def test_system_prompt_master_excludes_deflection(self):
        from bot.ai.prompts import _build_system_prompt, _style_rules_for_level

        master_prompt = _build_system_prompt(
            style_rules=_style_rules_for_level(LEVEL_SUPER))
        member_prompt = _build_system_prompt(
            style_rules=_style_rules_for_level(LEVEL_MEMBER))
        self.assertNotIn("你自己搜下呗", master_prompt)
        self.assertIn("你自己搜下呗", member_prompt)

    def test_tool_rules_prefer_tools_for_facts(self):
        from bot.ai.prompts import TOOL_USAGE_RULES

        self.assertIn("优先调工具", TOOL_USAGE_RULES)

    def test_private_chat_rules_prefer_tools(self):
        from bot.ai import runtime

        source = inspect.getsource(runtime.handle_ai_chat)
        self.assertIn("先调用工具查", source)
        self.assertNotIn("遇到不确定的事就说不知道", source)

    def test_capability_overview_by_level(self):
        from bot.ai.prompts import _capability_overview

        member = _capability_overview(LEVEL_MEMBER)
        master = _capability_overview(LEVEL_SUPER)
        self.assertIn("get_bot_help", member)
        self.assertNotIn("/master", member)
        self.assertNotIn("群管功能", member)
        self.assertIn("/master", master)
        self.assertIn("群管功能", master)
        self.assertLessEqual(len(master.splitlines()), 8)


class LegacyToolLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_keywords_cover_search_and_time(self):
        from bot.ai.tools import _READ_TOOL_SPEC, _should_consider_napcat_tool

        self.assertTrue(_should_consider_napcat_tool("帮我搜索一下资料"))
        self.assertTrue(_should_consider_napcat_tool("查一下天气"))
        self.assertTrue(_should_consider_napcat_tool("现在几点了"))
        self.assertIn("uapi_search", _READ_TOOL_SPEC)

    async def test_execute_tool_allows_uapi_search(self):
        import ai_tools

        dispatcher = type("Stub", (), {"config": {}})()
        fake = {"ok": True, "data": {"results": []}}
        with patch.object(ai_tools, "uapi_search",
                          new=AsyncMock(return_value=dict(fake))):
            result = await ai_tools.execute_tool(
                dispatcher, "uapi_search", {"query": "测试"})
        self.assertTrue(result["ok"])


class GetBotHelpTests(unittest.IsolatedAsyncioTestCase):
    COMMANDS = {
        "天气": {"help": "查天气 /天气 城市"},
        "master": {"help": "管理群主人 /master add QQ号", "bot_owner_only": True},
        "kick": {"help": "踢人 /kick @某人", "admin_only": True},
    }

    def test_digest_detail_filters_by_level(self):
        from bot.commands.system import build_help_digest

        status, _name, _text = build_help_digest(
            self.COMMANDS, LEVEL_MEMBER, "master", group_id=100)
        self.assertEqual(status, "denied")
        status, _name, text = build_help_digest(
            self.COMMANDS, LEVEL_SUPER, "master", group_id=100)
        self.assertEqual(status, "ok")
        self.assertIn("/master add", text)

    def test_digest_overview_filters_by_level(self):
        from bot.commands.system import build_help_digest

        _status, _name, member_text = build_help_digest(
            self.COMMANDS, LEVEL_MEMBER, "", group_id=100)
        self.assertNotIn("/master", member_text)
        self.assertNotIn("/kick", member_text)
        self.assertIn("/天气", member_text)
        _status, _name, super_text = build_help_digest(
            self.COMMANDS, LEVEL_SUPER, "", group_id=100)
        self.assertIn("/master", super_text)
        self.assertIn("/kick", super_text)

    def test_digest_category_query(self):
        from bot.commands.system import build_help_digest

        status, _name, text = build_help_digest(
            self.COMMANDS, LEVEL_ADMIN, "群管理", group_id=100)
        self.assertEqual(status, "ok")
        self.assertIn("/kick", text)
        status, _name, _text = build_help_digest(
            self.COMMANDS, LEVEL_MEMBER, "群管理", group_id=100)
        self.assertEqual(status, "denied")

    def test_digest_not_found(self):
        from bot.commands.system import build_help_digest

        status, _name, _text = build_help_digest(
            self.COMMANDS, LEVEL_SUPER, "不存在", group_id=100)
        self.assertEqual(status, "not_found")

    async def test_help_command_output_unchanged(self):
        from bot.commands.system import COMMAND_DETAILS, cmd_help

        replies = []
        dispatcher = type("Dispatcher", (), {})()
        dispatcher.commands = dict(self.COMMANDS)
        dispatcher.config = {"bot_owner": 100, "bot_qq": 200}

        async def reply(*args, **kwargs):
            replies.append((args, kwargs))

        dispatcher._reply = reply
        with patch("bot.commands.system.get_user_level", new=AsyncMock(
                return_value=(LEVEL_SUPER, "super"))), patch(
                "bot.commands.system.get_bot_role", new=AsyncMock(
                    return_value=("owner", "owner"))):
            await cmd_help(dispatcher, 100, 100, "master", "owner", "主人", [])
        self.assertEqual(len(replies), 1)
        self.assertIn(COMMAND_DETAILS["master"], replies[0][0][2])

    async def test_execute_get_bot_help_tool(self):
        import ai_tools

        dispatcher = type("Dispatcher", (), {})()
        dispatcher.config = {"bot_owner": 1, "bot_qq": 2}
        dispatcher.commands = dict(self.COMMANDS)

        result = await ai_tools.execute_ai_tool(
            dispatcher, "get_bot_help", {"command_or_category": "master"},
            group_id=0, user_id=1)
        self.assertTrue(result["ok"])
        self.assertIn("/master add", result["data"])

        result = await ai_tools.execute_ai_tool(
            dispatcher, "get_bot_help", {"command_or_category": "master"},
            group_id=0, user_id=9)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "help_denied")

        result = await ai_tools.execute_ai_tool(
            dispatcher, "get_bot_help", {}, group_id=0, user_id=9)
        self.assertTrue(result["ok"])
        self.assertNotIn("/master", result["data"])


class MemoryToleranceTests(unittest.TestCase):
    def test_sanitize_drops_entries_missing_content(self):
        from bot.ai.memory import _sanitize_entries

        entries = [
            {"role": "user"},                                  # 缺 content，丢弃
            {"content": "长期摘要", "ts": 1},                   # 长期记忆形态，保留
            "junk",
            {"role": "user", "content": "你好", "ts": 1},
        ]
        sanitized = _sanitize_entries(entries)
        self.assertEqual(len(sanitized), 2)
        self.assertEqual(sanitized[0]["content"], "长期摘要")
        self.assertEqual(sanitized[1]["role"], "user")

    def test_load_memory_tolerates_corrupt_entries(self):
        from bot.ai import memory

        group_id = 987654
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "group_{}.json".format(group_id)
            path.write_text(json.dumps([
                {"role": "user"},
                {"role": "user", "content": "正常条目", "ts": time.time()},
            ]), encoding="utf-8")
            with patch.object(memory, "MEMORY_DIR", root):
                memory._memories.pop(group_id, None)
                loaded = memory._load_memory(group_id)
            memory._memories.pop(group_id, None)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["content"], "正常条目")


class SmallFixesTests(unittest.IsolatedAsyncioTestCase):
    def test_name_mention_ignores_empty_names(self):
        from bot.events.message import GroupMessageMixin

        stub = type("Stub", (), {"config": {
            "name_mention": {"enabled": True, "names": ["", "小汐"]}}})()
        self.assertFalse(GroupMessageMixin._check_name_mention(stub, "随便聊聊"))
        self.assertTrue(GroupMessageMixin._check_name_mention(stub, "小汐在吗"))

    def test_blacklist_protects_owner_across_str_int(self):
        from bot import guard

        with patch.object(guard, "load_blacklist", return_value={}), \
                patch.object(guard, "save_blacklist") as save:
            guard.add_blacklist(100, "1", bot_owner=1, bot_qq=2)
            save.assert_not_called()
            guard.add_blacklist(100, "2", bot_owner=1, bot_qq=2)
            save.assert_not_called()
            guard.add_blacklist(100, "9", bot_owner=1, bot_qq=2)
            save.assert_called_once()

    async def test_request_comment_sanitized_in_log(self):
        from bot.events import request as request_module

        dispatcher = type("Stub", (), {"config": {
            "bot_owner": 1, "bot_qq": 2,
            "groups": {"100": {"enabled": True}},
            "group_defaults": {},
        }})()
        event = {
            "request_type": "group", "sub_type": "add", "group_id": 100,
            "user_id": 9, "flag": "flag123",
            "comment": "我是12345678\n伪造日志行",
        }
        with patch.object(request_module, "is_blacklisted", return_value=False), \
                patch.object(request_module, "load_pending_requests",
                             return_value={}), \
                patch.object(request_module, "save_pending_requests"), \
                self.assertLogs("qqbot", level="INFO") as captured:
            await request_module.handle_request(dispatcher, event)
        joined = "\n".join(captured.output)
        # QQ 号形态的数字被脱敏，换行被去掉（不伪造日志行）
        self.assertNotIn("12345678", joined)
        self.assertIn("<id>", joined)
        self.assertTrue(all("\n" not in entry for entry in captured.output))

    def test_chat_log_nickname_sanitized(self):
        from bot.events import context as context_module

        dispatcher = type("Stub", (), {"config": {
            "bot_owner": 1, "bot_qq": 2,
            "groups": {"100": {"enabled": True}},
            "group_defaults": {},
        }})()
        with patch.object(context_module, "is_group_enabled", return_value=True), \
                self.assertLogs("qqbot.chat", level="INFO") as captured:
            context_module._log_chat_message(
                dispatcher, "GROUP_IN", "hi", group_id=100, user_id=9,
                sender_name="坏人12345678\n伪造行")
        joined = "\n".join(captured.output)
        self.assertNotIn("12345678", joined)
        self.assertIn("坏人<id>", joined)
        self.assertTrue(all("\n" not in entry for entry in captured.output))


if __name__ == "__main__":
    unittest.main()
