import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from bot import bilibili, scheduler
from main import load_config


class ConfigRecoveryTests(unittest.TestCase):
    def test_invalid_config_recovers_from_last_good(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            backup = path + ".last-good"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"broken": true} trailing')
            expected = {"ws_url": "ws://127.0.0.1:3001", "token": ""}
            with open(backup, "w", encoding="utf-8") as handle:
                json.dump(expected, handle)

            loaded = load_config(path)

            self.assertEqual(loaded, expected)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), expected)

    def test_invalid_config_without_backup_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not-json")

            with self.assertRaisesRegex(RuntimeError, "no valid last-good backup"):
                load_config(path)


class SchedulerReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_acg_without_key_skips_resolution(self):
        class Client:
            is_connected = True

            async def send_group_forward_msg(self, group_id, nodes):
                raise AssertionError("no message should be sent")

        class Stub:
            config = {
                "bot_qq": 1,
                "acg_images": {"enabled": True, "count": 50},
                "groups": {"100": {"enabled": True, "features": {}}},
            }
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            resolver = AsyncMock(return_value="unused")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path),                     patch("bot.uapi.uapi_resolve_image_url", resolver):
                await scheduler._daily_acg_push(Stub())
            resolver.assert_not_awaited()

    async def test_acg_pending_retries_without_key(self):
        sent = []

        class Client:
            is_connected = True

            async def send_group_forward_msg(self, group_id, nodes):
                sent.append((group_id, nodes))
                return {"status": "ok", "retcode": 0}

        class Stub:
            config = {
                "bot_qq": 1,
                "acg_images": {"enabled": True, "count": 50},
                "groups": {"100": {"enabled": True, "features": {}}},
            }
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path):
                scheduler._save_acg_history([], {"100": ["https://example.com/a.jpg"]})
                await scheduler._daily_acg_push(Stub())
                self.assertNotIn("100", scheduler._load_acg_state()["pending"])
        self.assertEqual(len(sent), 1)

    async def test_checkin_skips_when_onebot_offline(self):
        class Client:
            is_connected = False

            async def send_group_sign(self, group_id):
                raise AssertionError("offline task must not call OneBot")

        class Stub:
            config = {}
            client = Client()

        self.assertEqual(await scheduler._run_group_checkin(Stub(), ["100"], "daily"), {})


class BilibiliCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_risk_control_pauses_official_api(self):
        class Client:
            session = object()

        class Stub:
            config = {
                "bilibili": {"official_retries": 2, "risk_cooldown_seconds": 600},
                "uapi_api_key": "",
            }
            client = Client()

        bilibili.reset_state_for_test()
        bilibili._state["img_key"] = "a" * 32
        bilibili._state["sub_key"] = "b" * 32
        api_call = AsyncMock(return_value={"code": -412})
        with patch.object(bilibili, "_ensure_session", new=AsyncMock(return_value=True)),                 patch.object(bilibili, "_bili_get", new=api_call):
            self.assertEqual(await bilibili.get_archives(Stub(), 123), [])
            self.assertEqual(await bilibili.get_archives(Stub(), 123), [])

        self.assertEqual(api_call.await_count, 1)
        self.assertGreater(bilibili._state["risk_until"], time.time())
