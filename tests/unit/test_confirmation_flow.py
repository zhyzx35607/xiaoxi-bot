"""Confirmation flow: any admin may confirm, non-admins cannot."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

OWNER = 1
BOT = 2
MASTER = 10
ADMIN_B = 20
MEMBER = 30
GROUP = 100


class _Client:
    def __init__(self):
        self.calls = []

    async def call(self, action, params=None):
        self.calls.append((action, params))
        return {"status": "ok", "data": {}}


class _Dispatcher:
    def __init__(self):
        from bot import permission
        permission._bot_role_cache.clear()
        self.config = {
            "bot_owner": OWNER,
            "bot_qq": BOT,
            "group_defaults": {},
            "groups": {str(GROUP): {"enabled": True, "masters": [MASTER]}},
        }
        self.client = _Client()


class ConfirmationAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def _confirm(self, dispatcher, code, user_id):
        from bot.services import confirmations
        return await confirmations.execute_confirmation(
            dispatcher, code, user_id, GROUP, "member")

    async def test_non_admin_cannot_confirm(self):
        from bot.services import confirmations

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending.json"
            with patch.object(confirmations, "_PATH", str(path)):
                d = _Dispatcher()
                code = confirmations.create_confirmation(
                    GROUP, MASTER, "set_group_name", {"group_id": GROUP}, "rename")
                ok, message = await self._confirm(d, code, MEMBER)
            self.assertFalse(ok)
            self.assertIn("管理员", message)

    async def test_other_admin_can_confirm(self):
        # 放宽发起人绑定：任意管理员可确认，避免普通成员发起的计划死锁。
        from bot.services import confirmations

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending.json"
            with patch.object(confirmations, "_PATH", str(path)):
                d = _Dispatcher()
                code = confirmations.create_confirmation(
                    GROUP, MEMBER, "set_group_name", {"group_id": GROUP}, "test")
                # MEMBER 自己不能确认（非管理）
                ok, message = await self._confirm(d, code, MEMBER)
                self.assertFalse(ok)
                self.assertIn("管理员", message)
                # 群主人（非发起人）确认成功并执行了动作
                ok, message = await self._confirm(d, code, MASTER)
            self.assertTrue(ok, message)
            self.assertEqual([("set_group_name", {"group_id": GROUP})], d.client.calls)


if __name__ == "__main__":
    unittest.main()
