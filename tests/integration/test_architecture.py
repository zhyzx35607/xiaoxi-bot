"""Architecture and compatibility boundary tests."""

import os
import subprocess
import tempfile
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
        from bot import security as legacy_security
        from bot.security import extract_urls
        from bot.security.core import extract_urls as core_extract_urls
        from bot.transport.onebot import OneBotClient as transport_client
        from bot.integrations.napcat import get_websocket_url

        self.assertIs(handle_ai_chat, runtime_chat)
        self.assertIs(register_all, registry_register_all)
        self.assertIs(OneBotClient, transport_client)
        self.assertIs(extract_urls, core_extract_urls)
        self.assertTrue(hasattr(legacy_security, "check_message_urls"))
        self.assertTrue(callable(get_websocket_url))
        self.assertTrue(callable(Dispatcher.dispatch))

    def test_historical_monoliths_remain_thin(self):
        budgets = {
            "main.py": 80,
            # ai/runtime and security/core grew slightly for injection
            # screening of history/memory and punish-failure isolation.
            "bot/ai/runtime.py": 850,
            "bot/commands/runtime.py": 100,
            "bot/dispatcher.py": 800,
            "bot/bilibili.py": 80,
            "bot/scheduler.py": 80,
            "bot/touchgal.py": 80,
            "bot/uapi.py": 80,
            "bot/security/core.py": 275,
            "deploy/napcat-login-watchdog.py": 80,
        }
        for relative_path, limit in budgets.items():
            with self.subTest(path=relative_path):
                path = os.path.join(ROOT, *relative_path.split("/"))
                with open(path, encoding="utf-8") as handle:
                    self.assertLessEqual(sum(1 for _ in handle), limit)

    def test_removed_security_monolith_does_not_return(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "bot", "security.py")))

    def test_compat_facades_resolve_mutable_state_live(self):
        from bot import bilibili as bilibili_facade
        from bot import uapi as uapi_facade
        from bot.integrations import bilibili as bilibili_impl
        from bot.integrations import uapi as uapi_impl

        uapi_impl._state = {"marker": True}
        self.assertIs(uapi_facade._state, uapi_impl._state)
        uapi_facade.reset_state_for_test()
        self.assertIsNone(uapi_facade._state)
        self.assertIsNone(uapi_impl._state)

        self.assertIs(bilibili_facade._state, bilibili_impl._state)
        bilibili_facade.reset_state_for_test()
        self.assertIs(bilibili_facade._state, bilibili_impl._state)

    def test_runtime_paths_remain_compatible(self):
        from bot.integrations import bilibili, uapi
        from bot.services import scheduler

        self.assertEqual(os.path.dirname(bilibili._PUSH_STATE_PATH), os.path.join(ROOT, "data"))
        self.assertEqual(os.path.dirname(uapi._STATE_PATH), os.path.join(ROOT, "data"))
        self.assertEqual(os.path.dirname(scheduler._CHECKIN_STATUS_PATH), os.path.join(ROOT, "data"))
        with open(os.path.join(ROOT, "deploy", "qqbot.service"), encoding="utf-8") as handle:
            self.assertIn("/opt/qqbot/main.py", handle.read())

    def test_runtime_temp_files_stay_out_of_code_tree(self):
        for relative_path in ("bot/integrations/bilibili.py", "bot/commands/queries.py"):
            path = os.path.join(ROOT, *relative_path.split("/"))
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn('os.path.join(_ROOT, "tmp")', source)
        with open(os.path.join(ROOT, "deploy", "qqbot.service"), encoding="utf-8") as handle:
            service = handle.read()
        self.assertIn("QQBOT_TMP_DIR=/opt/qqbot/data/tmp", service)
        self.assertIn("QQBOT_DIAGNOSTICS_DIR=/opt/qqbot/data/diagnostics", service)
        self.assertIn("QQBOT_DISABLE_CHAT_LOG=1", service)
        with open(os.path.join(ROOT, "deploy", "qqbot-journald.conf"), encoding="utf-8") as handle:
            journal_config = handle.read()
        self.assertIn("SystemMaxUse=192M", journal_config)
        self.assertIn("MaxRetentionSec=7day", journal_config)

    def test_napcat_service_filters_logs_and_keeps_account_out_of_git(self):
        with open(os.path.join(ROOT, "deploy", "napcat.service"), encoding="utf-8") as handle:
            service = handle.read()
        self.assertIn("napcat_log_filter.py", service)
        self.assertIn("EnvironmentFile=-/etc/napcat.env", service)
        self.assertIn("User=napcat", service)
        self.assertIn("HOME=/var/lib/napcat", service)
        self.assertNotIn("User=root", service)
        self.assertNotRegex(service, r"-q\s+\d+")
        for relative_path in (
            "bot/integrations/napcat/watchdog.py",
            "rollback-roleplay.sh",
        ):
            with open(os.path.join(ROOT, *relative_path.split("/")), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotRegex(source, r"(?:onebot11|napcat)_\d{5,12}\.json")
        with open(os.path.join(ROOT, "deploy", "napcat-login-watchdog.service"), encoding="utf-8") as handle:
            watchdog_service = handle.read()
        self.assertIn("WorkingDirectory=/opt/qqbot", watchdog_service)
        self.assertIn("PYTHONPATH=/opt/qqbot", watchdog_service)
        self.assertIn("/opt/qqbot/deploy/napcat-login-watchdog.py", watchdog_service)
        self.assertIn("Type=simple", watchdog_service)
        self.assertIn("User=napcat", watchdog_service)
        self.assertIn("NoNewPrivileges=true", watchdog_service)
        self.assertIn("ProtectSystem=strict", watchdog_service)
        self.assertIn("CapabilityBoundingSet=", watchdog_service)
        self.assertFalse(os.path.exists(os.path.join(
            ROOT, "deploy", "napcat-login-watchdog.timer"
        )))
        with open(os.path.join(ROOT, "deploy", "napcat-restart.path"), encoding="utf-8") as handle:
            restart_path = handle.read()
        self.assertIn("restart.request", restart_path)
        self.assertIn("napcat-restart.service", restart_path)
    def test_no_dependency_was_added_for_refactor(self):
        with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as handle:
            requirements = {line.strip() for line in handle if line.strip()}
        self.assertEqual(requirements, {"aiohttp==3.14.3", "websockets==16.1.1"})

    def test_rollback_restores_previous_git_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            def run(*args):
                subprocess.run(
                    ["git", *args], cwd=directory, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

            run("init", "-q")
            run("config", "user.email", "qqbot-tests@example.invalid")
            run("config", "user.name", "QQ Bot Tests")
            with open(os.path.join(directory, "sentinel.txt"), "w", encoding="utf-8") as handle:
                handle.write("before")
            run("add", "sentinel.txt")
            run("commit", "-qm", "before")
            with open(os.path.join(directory, "sentinel.txt"), "w", encoding="utf-8") as handle:
                handle.write("after")
            with open(os.path.join(directory, "new.txt"), "w", encoding="utf-8") as handle:
                handle.write("new")
            run("add", ".")
            run("commit", "-qm", "after")
            run("reset", "--hard", "HEAD~1")
            with open(os.path.join(directory, "sentinel.txt"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "before")
            self.assertFalse(os.path.exists(os.path.join(directory, "new.txt")))


if __name__ == "__main__":
    unittest.main()
