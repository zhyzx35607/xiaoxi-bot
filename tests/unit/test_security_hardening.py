"""Regression tests for command scope and destructive-operation hardening."""

import json
import re
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class _Dispatcher:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or type("Client", (), {})()
        self.replies = []

    async def _reply(self, *args, **kwargs):
        self.replies.append((args, kwargs))


class PermissionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_hint_is_denied_when_member_api_fails(self):
        from bot.permission import check_permission

        client = type("Client", (), {
            "get_group_member_info": AsyncMock(side_effect=RuntimeError("offline")),
        })()
        dispatcher = _Dispatcher({
            "bot_owner": 1, "bot_qq": 2, "groups": {"100": {}},
            "group_defaults": {},
        }, client)

        allowed, error = await check_permission(
            dispatcher, 100, 9, "admin", {"admin_only": True})

        self.assertFalse(allowed)
        self.assertIn("核验", error)

    async def test_unknown_target_role_blocks_moderation(self):
        from bot.permission import can_moderate_target

        client = type("Client", (), {
            "get_group_member_info": AsyncMock(side_effect=RuntimeError("offline")),
        })()
        dispatcher = _Dispatcher({
            "bot_owner": 1, "bot_qq": 2,
            "groups": {"100": {"masters": [10]}}, "group_defaults": {},
        }, client)

        allowed, error = await can_moderate_target(
            dispatcher, 100, 10, 99, "member")

        self.assertFalse(allowed)
        self.assertIn("目标", error)


class SecurityCheckFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_sender_role_prevents_auto_punishment(self):
        from bot.security.core import _can_punish

        client = type("Client", (), {
            "get_group_member_info": AsyncMock(side_effect=RuntimeError("offline")),
        })()
        dispatcher = _Dispatcher({
            "bot_owner": 1, "bot_qq": 2,
            "groups": {"100": {}}, "group_defaults": {},
        }, client)

        allowed, reason = await _can_punish(
            dispatcher, 100, 9, "admin")

        self.assertFalse(allowed)
        self.assertEqual(reason, "user_role_unverified")

    async def test_url_checker_exception_stops_bot_processing_without_punishment(self):
        from bot.security.core import check_message_urls

        client = type("Client", (), {
            "check_url_safely": AsyncMock(side_effect=RuntimeError("offline")),
            "delete_msg": AsyncMock(),
            "set_group_ban": AsyncMock(),
        })()
        dispatcher = _Dispatcher({
            "bot_owner": 1, "bot_qq": 2, "security": {},
            "groups": {"100": {}}, "group_defaults": {},
        }, client)

        with patch("bot.security.core.record_security_event") as record:
            blocked = await check_message_urls(
                dispatcher, 100, 9, "see https://example.invalid", 55)

        self.assertTrue(blocked)
        client.delete_msg.assert_not_awaited()
        client.set_group_ban.assert_not_awaited()
        self.assertEqual(record.call_args.args[1], "url_check_failed")

    def test_unverified_url_results_are_not_treated_as_safe(self):
        from bot.security.core import is_url_check_risky

        self.assertIsNone(is_url_check_risky(None)[0])
        self.assertIsNone(is_url_check_risky({"status": "failed"})[0])
        self.assertIsNone(is_url_check_risky({"status": "ok", "data": {}})[0])

    def test_extract_urls_does_not_drop_urls_beyond_five(self):
        from bot.security.core import extract_urls

        text = " ".join("https://example{}.invalid/x".format(i) for i in range(8))
        self.assertEqual(len(extract_urls(text)), 8)


class GroupScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_commands_reject_cross_group_targets(self):
        from bot.commands import admin, system

        config = {
            "bot_owner": 1, "bot_qq": 2, "group_defaults": {},
            "groups": {"100": {"enabled": True}, "200": {"enabled": True}},
        }
        for command in (admin.cmd_enable, admin.cmd_disable):
            dispatcher = _Dispatcher(config)
            with patch.object(admin, "_load", return_value=config), \
                    patch.object(admin, "_save") as save:
                await command(dispatcher, 100, 10, "200", "member", "", [])
            save.assert_not_called()
            self.assertIn("当前群", dispatcher.replies[-1][0][2])

        dispatcher = _Dispatcher(config)
        with patch.object(system, "create_confirmation") as create:
            await system.cmd_clear_ai(
                dispatcher, 100, 10, "200", "member", "", [])
        create.assert_not_called()
        self.assertIn("当前群", dispatcher.replies[-1][0][2])

    async def test_private_clear_requires_explicit_targets(self):
        from bot.commands import system

        dispatcher = _Dispatcher({
            "bot_owner": 1, "bot_qq": 2, "groups": {"100": {}},
        })
        with patch.object(system, "create_confirmation") as create:
            await system.cmd_clear_ai(
                dispatcher, None, 1, "", "member", "", [])
        create.assert_not_called()
        self.assertIn("明确", dispatcher.replies[-1][0][2])


class ClearDataConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_waits_for_confirmation_and_creates_backup(self):
        from bot import guard
        from bot.ai import memory
        from bot.commands import system
        from bot.services import confirmations, group_data

        with tempfile.TemporaryDirectory() as root:
            data_root = Path(root) / "data"
            memories = data_root / "memories"
            stickers = data_root / "stickers"
            memories.mkdir(parents=True)
            stickers.mkdir()
            pending = data_root / "pending_actions.json"
            blacklist = data_root / "blacklist.json"
            warnings = data_root / "r18_warnings.json"
            files = {
                memories / "group_100.json": "[]",
                memories / "group_100_long.json": "[]",
                memories / "group_100_u9.json": "[]",
                memories / "group_100_u9_long.json": "[]",
                stickers / "group_100.json": "[]",
            }
            for path, content in files.items():
                path.write_text(content, encoding="utf-8")
            blacklist.write_text(json.dumps({"100_9": {}, "200_9": {}}), encoding="utf-8")
            warnings.write_text(json.dumps({"100_9": [1], "200_9": [1]}), encoding="utf-8")
            dispatcher = _Dispatcher({
                "bot_owner": 1, "bot_qq": 2,
                "groups": {"100": {"masters": [10]}, "200": {}},
                "group_defaults": {},
            })

            with patch.object(confirmations, "_PATH", str(pending)), \
                    patch.object(group_data, "_DATA_ROOT", data_root), \
                    patch.object(group_data, "_BACKUP_DIR", data_root / "operation_backups"), \
                    patch.object(memory, "MEMORY_DIR", str(memories)), \
                    patch.object(guard, "BLACKLIST_FILE", str(blacklist)), \
                    patch.object(guard, "R18_WARNING_FILE", str(warnings)), \
                    patch.object(guard, "_bl_cache", None), \
                    patch.object(guard, "_warn_cache", None):
                await system.cmd_clear_ai(
                    dispatcher, 100, 10, "", "member", "", [])
                self.assertTrue(all(path.exists() for path in files))
                code = re.search(r"/确认 ([0-9a-f]{8})", dispatcher.replies[-1][0][2]).group(1)

                ok, message = await confirmations.execute_confirmation(
                    dispatcher, code, 10, 100, "member")

            self.assertTrue(ok, message)
            self.assertTrue(all(not path.exists() for path in files))
            self.assertEqual(json.loads(blacklist.read_text(encoding="utf-8")), {"200_9": {}})
            self.assertEqual(json.loads(warnings.read_text(encoding="utf-8")), {"200_9": [1]})
            backups = list((data_root / "operation_backups").glob("clearai-*.tar.gz"))
            self.assertEqual(len(backups), 1)
            with tarfile.open(backups[0], "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("manifest.json", names)
            self.assertIn("memories/group_100.json", names)
            self.assertIn("blacklist.json", names)

    async def test_tampered_group_target_is_rejected(self):
        from bot.services import confirmations

        with tempfile.TemporaryDirectory() as root:
            pending = Path(root) / "pending.json"
            dispatcher = _Dispatcher({
                "bot_owner": 1, "bot_qq": 2,
                "groups": {"100": {"masters": [10]}, "200": {}},
                "group_defaults": {},
            })
            with patch.object(confirmations, "_PATH", str(pending)):
                code = confirmations.create_confirmation(
                    100, 10, "__clear_group_data__",
                    {"group_ids": ["100"]}, "clear")
                data = json.loads(pending.read_text(encoding="utf-8"))
                data[code]["params"]["group_ids"] = ["200"]
                pending.write_text(json.dumps(data), encoding="utf-8")

                ok, message = await confirmations.execute_confirmation(
                    dispatcher, code, 10, 100, "member")

            self.assertFalse(ok)
            self.assertIn("校验失败", message)


class CapabilityWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from bot import permission
        permission._bot_role_cache.clear()

    def _dispatcher(self, caller_role="member", bot_role="owner"):
        async def member_info(_group_id, user_id):
            role = bot_role if user_id == 2 else caller_role
            return {"status": "ok", "data": {"role": role}}

        client = type("Client", (), {})()
        client.get_group_member_info = AsyncMock(side_effect=member_info)
        for name in (
            "set_group_todo", "send_flash_msg", "upload_group_album_image",
            "comment_group_album_media", "set_group_album_media_like",
            "delete_friend", "call", "get_msg",
        ):
            setattr(client, name, AsyncMock(return_value={"status": "ok", "data": {}}))
        config = {
            "bot_owner": 1, "bot_qq": 2, "group_defaults": {},
            "groups": {"100": {"napcat_features": {
                "todo": True, "message": True, "album": True,
                "interaction": True, "friend": True,
            }}},
        }
        return _Dispatcher(config, client)

    async def test_member_cannot_mutate_todos_or_send_flash(self):
        from bot.commands.capabilities import (
            cmd_interaction_center, cmd_message_center, cmd_todo_center,
        )

        dispatcher = self._dispatcher("member")
        await cmd_todo_center(dispatcher, 100, 9, "添加 55", "member", "", [])
        await cmd_message_center(dispatcher, 100, 9, "闪传 set1", "member", "", [])
        await cmd_interaction_center(dispatcher, 100, 9, "闪传 set1", "member", "", [])

        dispatcher.client.set_group_todo.assert_not_awaited()
        dispatcher.client.send_flash_msg.assert_not_awaited()

    async def test_album_rejects_local_path_and_preserves_comment_text(self):
        from bot.commands.capabilities import cmd_album_center

        dispatcher = self._dispatcher("admin")
        await cmd_album_center(
            dispatcher, 100, 9, r"上传 a1 相册 C:\secret.txt", "admin", "", [])
        dispatcher.client.upload_group_album_image.assert_not_awaited()

        await cmd_album_center(
            dispatcher, 100, 9, "评论 a1 lloc1 好", "admin", "", [])
        dispatcher.client.comment_group_album_media.assert_awaited_once_with(
            100, "a1", "lloc1", "好")
        dispatcher.client.comment_group_album_media.reset_mock()

        await cmd_album_center(
            dispatcher, 100, 9, "评论 a1 lloc1 这是一条 完整评论", "admin", "", [])
        dispatcher.client.comment_group_album_media.assert_awaited_once_with(
            100, "a1", "lloc1", "这是一条 完整评论")

    async def test_write_is_denied_when_bot_is_not_group_admin(self):
        from bot.commands.capabilities import cmd_todo_center

        dispatcher = self._dispatcher("admin", bot_role="member")
        await cmd_todo_center(
            dispatcher, 100, 9, "添加 55", "admin", "", [])

        dispatcher.client.set_group_todo.assert_not_awaited()
        self.assertIn("我现在不是管理员", dispatcher.replies[-1][0][2])

    async def test_album_accepts_public_url_and_replied_image(self):
        from bot.commands import capabilities

        dispatcher = self._dispatcher("admin")
        with patch.object(
                capabilities, "_resolved_public_url",
                new=AsyncMock(side_effect=lambda value: value)):
            await capabilities.cmd_album_center(
                dispatcher, 100, 9,
                "上传 a1 相册 https://images.example/a.jpg", "admin", "", [])
            dispatcher.client.get_msg.return_value = {
                "status": "ok", "data": {"message": [{
                    "type": "image", "data": {"url": "https://images.example/b.jpg"},
                }]},
            }
            await capabilities.cmd_album_center(
                dispatcher, 100, 9, "上传 a1 相册", "admin", "",
                [{"type": "reply", "data": {"id": "55"}}])

        self.assertEqual(dispatcher.client.upload_group_album_image.await_count, 2)
        dispatcher.client.upload_group_album_image.assert_any_await(
            100, "a1", "相册", "https://images.example/b.jpg")

    async def test_friend_delete_requires_confirmation(self):
        from bot.commands import capabilities
        from bot.services import confirmations

        dispatcher = self._dispatcher("member")
        with tempfile.TemporaryDirectory() as root, \
                patch.object(confirmations, "_PATH", str(Path(root) / "pending.json")):
            await capabilities.cmd_friend_center(
                dispatcher, None, 1, "删除 123456", "member", "", [])

        dispatcher.client.delete_friend.assert_not_awaited()
        self.assertIn("/确认", dispatcher.replies[-1][0][2])


if __name__ == "__main__":
    unittest.main()
