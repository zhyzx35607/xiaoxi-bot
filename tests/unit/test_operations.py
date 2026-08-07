"""Operational command and diagnostics regression tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands.system import cmd_health


class HealthCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_uses_async_service_queries(self):
        dispatcher = SimpleNamespace(
            client=SimpleNamespace(_ws=object(), _event_tasks=set()),
            config={"bot_owner": 999},
            _background_tasks=set(),
            _reply=AsyncMock(),
        )
        memory = {
            "available": 512,
            "total": 1024,
            "swap_free": 128,
            "swap_total": 256,
        }

        with patch(
            "bot.commands.system._service_state",
            new=AsyncMock(side_effect=["active", "inactive"]),
        ) as service_state, patch(
            "bot.commands.system._read_linux_memory",
            return_value=memory,
        ):
            await cmd_health(dispatcher, None, 100, "", "member", "", [])

        self.assertEqual(service_state.await_count, 2)
        reply = dispatcher._reply.await_args.args[2]
        self.assertIn("小汐: active", reply)
        self.assertIn("NapCat: inactive", reply)
        self.assertIn("内存: 可用512M/总1024M", reply)


if __name__ == "__main__":
    unittest.main()
