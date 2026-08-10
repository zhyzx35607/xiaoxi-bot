"""Operational command and diagnostics regression tests."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands.system import cmd_health
from bot.services.health import HealthServiceMixin


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


class HealthLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_service_task_remains_tracked(self):
        class Task:
            cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        class Stub(HealthServiceMixin):
            pass

        task = Task()
        dispatcher = Stub()
        dispatcher._scheduler_task = task
        with patch(
            "bot.services.health.asyncio.wait",
            new=AsyncMock(return_value=(set(), {task})),
        ):
            await dispatcher.stop_scheduler()

        self.assertTrue(task.cancelled)
        self.assertIs(dispatcher._scheduler_task, task)

    async def test_background_tasks_are_removed_after_cancellation(self):
        class Stub(HealthServiceMixin):
            pass

        dispatcher = Stub()
        dispatcher._background_tasks = set()
        dispatcher._max_background_tasks = 4
        task = dispatcher.create_background_task(
            asyncio.sleep(60), name="test-background")

        await dispatcher.stop_background_tasks()

        self.assertTrue(task.cancelled())
        self.assertEqual(dispatcher._background_tasks, set())


if __name__ == "__main__":
    unittest.main()
