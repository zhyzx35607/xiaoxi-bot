"""Architecture and compatibility boundary tests."""

import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ArchitectureRegressionTests(unittest.TestCase):
    def test_legacy_public_imports_resolve_to_canonical_implementations(self):
        from bot.ai import handle_ai_chat
        from bot.ai.runtime import handle_ai_chat as runtime_chat
        from bot.client import OneBotClient
        from bot.commands import register_all
        from bot.commands.registry import register_all as registry_register_all
        from bot.dispatcher import Dispatcher
        from bot.transport.onebot import OneBotClient as transport_client

        self.assertIs(handle_ai_chat, runtime_chat)
        self.assertIs(register_all, registry_register_all)
        self.assertIs(OneBotClient, transport_client)
        self.assertTrue(callable(Dispatcher.dispatch))

    def test_historical_monoliths_remain_thin(self):
        budgets = {
            "main.py": 80,
            "bot/ai/runtime.py": 800,
            "bot/commands/runtime.py": 100,
            "bot/dispatcher.py": 800,
            "bot/bilibili.py": 80,
            "bot/scheduler.py": 80,
            "bot/touchgal.py": 80,
            "bot/uapi.py": 80,
        }
        for relative_path, limit in budgets.items():
            with self.subTest(path=relative_path):
                path = os.path.join(ROOT, *relative_path.split("/"))
                with open(path, encoding="utf-8") as handle:
                    self.assertLessEqual(sum(1 for _ in handle), limit)

    def test_runtime_paths_remain_compatible(self):
        from bot.integrations import bilibili, uapi
        from bot.services import scheduler

        self.assertEqual(os.path.dirname(bilibili._PUSH_STATE_PATH), os.path.join(ROOT, "data"))
        self.assertEqual(os.path.dirname(uapi._STATE_PATH), os.path.join(ROOT, "data"))
        self.assertEqual(os.path.dirname(scheduler._CHECKIN_STATUS_PATH), os.path.join(ROOT, "data"))
        with open(os.path.join(ROOT, "deploy", "qqbot.service"), encoding="utf-8") as handle:
            self.assertIn("/opt/qqbot/main.py", handle.read())

    def test_no_dependency_was_added_for_refactor(self):
        with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as handle:
            requirements = {line.strip() for line in handle if line.strip()}
        self.assertEqual(requirements, {"aiohttp==3.14.3", "websockets==16.1.1"})


if __name__ == "__main__":
    unittest.main()
