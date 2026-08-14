"""Regression tests for private multi-group feature switches."""

import unittest
from unittest.mock import AsyncMock, patch


class _Dispatcher:
    def __init__(self, config):
        self.config = config
        self.replies = []

    async def _reply(self, *args, **kwargs):
        self.replies.append((args, kwargs))


class GroupFeatureSwitchTests(unittest.IsolatedAsyncioTestCase):
    def _config(self):
        return {
            "bot_owner": 1, "bot_qq": 2, "group_defaults": {},
            "groups": {
                "111": {"enabled": True, "features": {}},
                "222": {"enabled": True, "features": {}},
                "333": {"enabled": True, "features": {}},
            },
        }

    def _features(self, config, gid):
        return config["groups"][gid].get("features", {})

    async def _toggle(self, config, group_id, args):
        from bot.commands import admin

        dispatcher = _Dispatcher(config)
        with patch.object(admin, "_load", return_value=config), \
                patch.object(admin, "_commit") as commit:
            await admin._toggle_group_feature(
                dispatcher, group_id, 1, args,
                "acg_images", "每日ACG图推送", "acg图")
        return dispatcher, commit

    def _last_reply(self, dispatcher):
        return dispatcher.replies[-1][0][2]

    async def test_private_multi_group_space_separated(self):
        config = self._config()
        dispatcher, commit = await self._toggle(config, None, "111 222 off")
        self.assertFalse(self._features(config, "111")["acg_images"])
        self.assertFalse(self._features(config, "222")["acg_images"])
        self.assertNotIn("acg_images", self._features(config, "333"))
        commit.assert_called_once()
        self.assertIn("已对 2 个群关闭每日ACG图推送：111、222",
                      self._last_reply(dispatcher))

    async def test_private_multi_group_comma_separated(self):
        config = self._config()
        dispatcher, commit = await self._toggle(config, None, "111,222，333 on")
        for gid in ("111", "222", "333"):
            self.assertTrue(self._features(config, gid)["acg_images"])
        commit.assert_called_once()
        self.assertIn("已对 3 个群开启每日ACG图推送：111、222、333",
                      self._last_reply(dispatcher))

    async def test_private_all_keyword_covers_configured_groups(self):
        config = self._config()
        dispatcher, commit = await self._toggle(config, None, "all off")
        for gid in ("111", "222", "333"):
            self.assertFalse(self._features(config, gid)["acg_images"])
        commit.assert_called_once()
        self.assertIn("已对 3 个群关闭每日ACG图推送", self._last_reply(dispatcher))

    async def test_private_skips_unconfigured_groups(self):
        config = self._config()
        dispatcher, commit = await self._toggle(config, None, "111 999 off")
        self.assertFalse(self._features(config, "111")["acg_images"])
        self.assertNotIn("999", config["groups"])
        commit.assert_called_once()
        reply = self._last_reply(dispatcher)
        self.assertIn("已对 1 个群关闭每日ACG图推送：111", reply)
        self.assertIn("跳过未配置群：999", reply)

    async def test_private_single_group_still_works(self):
        config = self._config()
        dispatcher, commit = await self._toggle(config, None, "111 on")
        self.assertTrue(self._features(config, "111")["acg_images"])
        commit.assert_called_once()
        self.assertIn("已对 1 个群开启每日ACG图推送：111",
                      self._last_reply(dispatcher))

    async def test_private_status_query_without_action(self):
        config = self._config()
        self._features(config, "111")["acg_images"] = False
        dispatcher, commit = await self._toggle(config, None, "111")
        commit.assert_not_called()
        self.assertIn("群111的每日ACG图推送：关闭", self._last_reply(dispatcher))

    async def test_in_group_usage_unchanged(self):
        config = self._config()
        dispatcher, commit = await self._toggle(config, 111, "off")
        self.assertFalse(self._features(config, "111")["acg_images"])
        commit.assert_called_once()
        self.assertEqual("本群每日ACG图推送已关闭", self._last_reply(dispatcher))

        config = self._config()
        dispatcher, commit = await self._toggle(config, 111, "")
        commit.assert_not_called()
        self.assertIn("本群每日ACG图推送：开启", self._last_reply(dispatcher))

    async def test_ai_chat_switch_supports_multi_group(self):
        from bot.commands import admin

        config = self._config()
        dispatcher = _Dispatcher(config)
        with patch.object(admin, "_load", return_value=config), \
                patch.object(admin, "_commit") as commit:
            await admin.cmd_group_ai_switch(
                dispatcher, None, 1, "111 222 off", "member", "", [])
        self.assertFalse(self._features(config, "111")["ai_chat"])
        self.assertFalse(self._features(config, "222")["ai_chat"])
        commit.assert_called_once()
        self.assertIn("已对 2 个群关闭AI聊天：111、222", self._last_reply(dispatcher))

    async def test_ai_chat_in_group_reply_unchanged(self):
        from bot.commands import admin

        config = self._config()
        dispatcher = _Dispatcher(config)
        with patch.object(admin, "_load", return_value=config), \
                patch.object(admin, "_commit"):
            await admin.cmd_group_ai_switch(
                dispatcher, 111, 1, "off", "member", "", [])
        self.assertFalse(self._features(config, "111")["ai_chat"])
        self.assertEqual("本群AI聊天已关闭", self._last_reply(dispatcher))


class PrivateSwitchDispatchTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self):
        from bot.events.message import PrivateMessageMixin

        handler = PrivateMessageMixin.__new__(PrivateMessageMixin)
        handler.config = {"bot_owner": 1, "bot_qq": 2, "groups": {"111": {}}}
        handler._reply = AsyncMock()
        handler._run_command = AsyncMock()
        return handler

    def test_switch_commands_registered_for_private_dispatch(self):
        from bot.events.message import PrivateMessageMixin

        multi = PrivateMessageMixin._private_multi_group_switch_names(None)
        self.assertEqual({"acg图", "热榜推送", "b站解析", "gal资源", "ai聊天"}, multi)
        self.assertTrue(
            multi <= PrivateMessageMixin._private_group_command_names(None))

    async def test_switch_command_passes_full_args_without_group(self):
        handler = self._handler()
        await handler._handle_owner_command(
            "acg图", "111 222 off", 1, {"nickname": "n"}, [], "/acg图 111 222 off")
        handler._run_command.assert_awaited_once_with(
            "acg图", "111 222 off", None, 1, "member", "n", [])

    async def test_other_group_commands_still_parse_single_group(self):
        handler = self._handler()
        await handler._handle_owner_command(
            "kick", "111 222", 1, {"nickname": "n"}, [], "/kick 111 222")
        handler._run_command.assert_awaited_once_with(
            "kick", "222", 111, 1, "member", "n", [])

    async def test_group_alias_passes_full_args(self):
        handler = self._handler()
        await handler._handle_owner_command(
            "group", "enable 111 222", 1, {"nickname": "n"}, [], "/group enable 111 222")
        handler._run_command.assert_awaited_once_with(
            "group", "enable 111 222", None, 1, "member", "n", [])


class GroupEnableAliasTests(unittest.IsolatedAsyncioTestCase):
    def _config(self):
        return {
            "bot_owner": 1, "bot_qq": 2, "group_defaults": {},
            "groups": {
                "111": {"enabled": False},
                "222": {"enabled": True},
                "333": {"enabled": True},
            },
        }

    async def _run(self, config, args):
        from bot.commands import admin

        dispatcher = _Dispatcher(config)
        with patch.object(admin, "_load", return_value=config), \
                patch.object(admin, "_commit"):
            await admin.cmd_group(dispatcher, None, 1, args, "member", "", [])
        return dispatcher

    async def test_group_enable_multiple_groups(self):
        config = self._config()
        dispatcher = await self._run(config, "enable 111 222")
        self.assertTrue(config["groups"]["111"]["enabled"])
        self.assertTrue(config["groups"]["222"]["enabled"])
        self.assertIn("已启用 2 个群", dispatcher.replies[-1][0][2])

    async def test_group_disable_all(self):
        config = self._config()
        await self._run(config, "disable all")
        for gid in ("111", "222", "333"):
            self.assertFalse(config["groups"][gid]["enabled"])

    async def test_group_unknown_subcommand_shows_usage(self):
        config = self._config()
        dispatcher = await self._run(config, "foo 111")
        self.assertIn("用法", dispatcher.replies[-1][0][2])

    async def test_group_rejects_non_owner_private(self):
        from bot.commands import admin

        config = self._config()
        dispatcher = _Dispatcher(config)
        with patch.object(admin, "_load", return_value=config), \
                patch.object(admin, "_commit") as commit:
            await admin.cmd_group(dispatcher, None, 999, "enable 111", "member", "", [])
        commit.assert_not_called()
        self.assertIn("最高主人", dispatcher.replies[-1][0][2])


if __name__ == "__main__":
    unittest.main()
