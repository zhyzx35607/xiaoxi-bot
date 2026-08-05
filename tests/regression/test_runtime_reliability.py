"""Runtime reliability and recovery regression tests."""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import bilibili, scheduler
import main as main_module
from main import load_config, migrate_config


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

    def test_atomic_json_write_keeps_file_private(self):
        from bot.utils import atomic_write_json

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "config.json")
            atomic_write_json(path, {"safe": True})
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), {"safe": True})
            if os.name != "nt":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class ContentConfigMigrationTests(unittest.TestCase):
    def test_random_content_defaults_are_added(self):
        config, migrated = migrate_config({})
        self.assertTrue(migrated)
        self.assertFalse(config["acg_images"]["enabled"])
        self.assertFalse(config["hotboard_push"]["enabled"])
        self.assertFalse(config["voice_reply"]["enabled"])
        self.assertEqual(config["acg_images"]["send_count"], 20)
        self.assertEqual(config["acg_images"]["dedupe_days"], 7)
        self.assertEqual(config["acg_images"]["max_delivery_attempts"], 3)
        self.assertEqual(config["acg_images"]["retry_base_seconds"], 300)
        self.assertEqual(config["acg_images"]["delivery_ttl_seconds"], 7200)
        self.assertEqual(len(config["acg_images"]["windows"]), 4)
        self.assertEqual(config["hotboard_push"]["detail_count"], 10)
        self.assertEqual(len(config["hotboard_push"]["windows"]), 2)
        self.assertNotIn("times", config["acg_images"])
        self.assertNotIn("count", config["acg_images"])
        self.assertNotIn("batch_size", config["acg_images"])
        self.assertNotIn("times", config["hotboard_push"])


class NapCatWatchdogTests(unittest.TestCase):
    def test_prefers_bot_websocket_port(self):
        script_path = Path(__file__).parents[2] / "deploy" / "napcat-login-watchdog.py"
        spec = importlib.util.spec_from_file_location("napcat_login_watchdog", script_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        config = {
            "network": {
                "websocketServers": [
                    {"enable": True, "host": "127.0.0.1", "port": 3002, "token": "observer"},
                    {"enable": True, "host": "127.0.0.1", "port": 3001, "token": "bot token"},
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "onebot.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(module, "CONFIG_PATH", path), \
                    patch.object(module, "PREFERRED_PORT", 3001):
                url = module.get_websocket_url()
        self.assertEqual(url, "ws://127.0.0.1:3001?access_token=bot%20token")



class RuntimeTemporaryFileTests(unittest.TestCase):
    def test_runtime_temp_file_uses_configured_writable_directory(self):
        from bot.storage.runtime_paths import create_runtime_temp_file, runtime_temp_dir

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"QQBOT_TMP_DIR": directory}):
                self.assertEqual(runtime_temp_dir(), directory)
                fd, path = create_runtime_temp_file("test_", ".bin")
                with os.fdopen(fd, "wb") as handle:
                    handle.write(b"safe")
                self.assertTrue(path.startswith(directory))
                self.assertEqual(Path(path).read_bytes(), b"safe")
                if os.name != "nt":
                    self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                os.remove(path)

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
                state = scheduler._load_acg_state()
                self.assertTrue(state["pending_due"])
                self.assertEqual(state["pool"], ["https://example.com/a.jpg"])
        self.assertEqual(len(sent), 0)

    async def test_acg_delivery_stops_after_max_attempts(self):
        sent = []
        notices = []

        class Client:
            is_connected = True

            async def send_group_forward_msg(self, group_id, nodes):
                sent.append(group_id)
                return {"status": "failed", "retcode": 1200}

            async def send_private_msg(self, user_id, message):
                notices.append((user_id, message))
                return {"status": "ok"}

        class Stub:
            config = {
                "bot_owner": 9,
                "bot_qq": 1,
                "acg_images": {
                    "enabled": True,
                    "max_delivery_attempts": 3,
                    "retry_base_seconds": 30,
                    "retry_max_seconds": 60,
                    "delivery_ttl_seconds": 7200,
                },
                "groups": {"100": {"enabled": True, "features": {"acg_images": True}}},
            }
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path), \
                    patch.object(scheduler.asyncio, "sleep", new=AsyncMock()):
                state = scheduler._new_acg_state()
                state["pending_due"] = True
                state["delivery"] = {
                    "batch_id": "test-batch",
                    "urls": ["https://example.com/a.jpg"],
                    "remaining_groups": ["100"],
                    "attempts": {},
                    "created_at": time.time(),
                    "next_retry_at": 0,
                }
                scheduler._save_acg_state(state)
                for _ in range(3):
                    await scheduler._try_send_acg_delivery(Stub())
                    state = scheduler._load_acg_state()
                    if state.get("delivery"):
                        state["delivery"]["next_retry_at"] = 0
                        scheduler._save_acg_state(state)
                state = scheduler._load_acg_state()

        self.assertEqual(sent, [100, 100, 100])
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["pending_due"])
        self.assertEqual(state["last_failure"]["reason"], "attempts_exhausted")
        self.assertEqual(state["last_failure"]["attempts"], {"100": 3})
        self.assertEqual(len(notices), 1)

    async def test_acg_delivery_checkpoints_each_group_before_restart(self):
        sent = []

        class Client:
            is_connected = True

            async def send_group_forward_msg(self, group_id, nodes):
                sent.append(group_id)
                return {"status": "ok", "retcode": 0}

            async def get_group_msg_history(self, group_id, count=20):
                return {"messages": []}

        class Stub:
            config = {
                "bot_qq": 1,
                "acg_images": {"enabled": True},
                "groups": {
                    "100": {"enabled": True, "features": {"acg_images": True}},
                    "200": {"enabled": True, "features": {"acg_images": True}},
                },
            }
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path):
                state = scheduler._new_acg_state()
                state["pending_due"] = True
                state["delivery"] = {
                    "batch_id": "restart-batch",
                    "urls": ["https://example.com/a.jpg"],
                    "remaining_groups": ["100", "200"],
                    "attempts": {},
                    "created_at": time.time(),
                    "next_retry_at": 0,
                }
                scheduler._save_acg_state(state)
                with patch.object(scheduler.asyncio, "sleep", new=AsyncMock(side_effect=scheduler.asyncio.CancelledError)):
                    with self.assertRaises(scheduler.asyncio.CancelledError):
                        await scheduler._try_send_acg_delivery(Stub())
                state = scheduler._load_acg_state()
                self.assertEqual(state["delivery"]["remaining_groups"], ["200"])
                with patch.object(scheduler.asyncio, "sleep", new=AsyncMock()):
                    await scheduler._try_send_acg_delivery(Stub())
                state = scheduler._load_acg_state()

        self.assertEqual(sent, [100, 200])
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["pending_due"])

    async def test_acg_delivery_recovers_accepted_inflight_batch_from_history(self):
        sent = []

        class Client:
            is_connected = True

            async def send_group_forward_msg(self, group_id, nodes):
                sent.append(group_id)
                return {"status": "ok"}

            async def get_group_msg_history(self, group_id, count=20):
                return {"messages": [{"raw_message": "batch #history-batch"}]}

        class Stub:
            config = {"bot_qq": 1, "acg_images": {"enabled": True}}
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path), \
                    patch.object(scheduler.asyncio, "sleep", new=AsyncMock()):
                state = scheduler._new_acg_state()
                state["pending_due"] = True
                state["delivery"] = {
                    "batch_id": "history-batch",
                    "urls": ["https://example.com/a.jpg"],
                    "remaining_groups": ["100"],
                    "attempts": {"100": 1},
                    "created_at": time.time(),
                    "next_retry_at": 0,
                }
                scheduler._save_acg_state(state)
                await scheduler._try_send_acg_delivery(Stub())
                state = scheduler._load_acg_state()

        self.assertEqual(sent, [])
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["pending_due"])

    async def test_acg_delivery_expires_without_sending(self):
        sent = []
        notices = []

        class Client:
            is_connected = True

            async def send_group_forward_msg(self, group_id, nodes):
                sent.append(group_id)
                return {"status": "ok"}

            async def send_private_msg(self, user_id, message):
                notices.append((user_id, message))
                return {"status": "ok"}

        class Stub:
            config = {
                "bot_owner": 9,
                "acg_images": {"enabled": True, "delivery_ttl_seconds": 300},
            }
            client = Client()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "acg.json")
            with patch.object(scheduler, "_ACG_HISTORY_PATH", path):
                state = scheduler._new_acg_state()
                state["pending_due"] = True
                state["delivery"] = {
                    "batch_id": "expired-batch",
                    "urls": ["https://example.com/a.jpg"],
                    "remaining_groups": ["100"],
                    "attempts": {},
                    "created_at": time.time() - 301,
                    "next_retry_at": 0,
                }
                scheduler._save_acg_state(state)
                await scheduler._try_send_acg_delivery(Stub())
                state = scheduler._load_acg_state()

        self.assertEqual(sent, [])
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["pending_due"])
        self.assertEqual(state["last_failure"]["reason"], "delivery_expired")
        self.assertEqual(len(notices), 1)

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

class ImportAndDeploymentTests(unittest.TestCase):
    def test_importing_main_does_not_initialize_file_logging(self):
        with patch("app.logging_setup.setup_logging") as setup:
            importlib.reload(main_module)
            setup.assert_not_called()
        importlib.reload(main_module)

    def test_runtime_config_migration_removes_env_managed_secrets(self):
        script_path = Path(__file__).parents[2] / "scripts" / "migrate_runtime_config.py"
        spec = importlib.util.spec_from_file_location("migrate_runtime_config", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = {
            "token": "persisted",
            "deepseek_api_key": "persisted",
            "vision_api": {"api_key": "persisted", "model": "vision"},
        }
        removed = module.remove_env_managed_secrets(config, {
            "QQBOT_TOKEN": "runtime",
            "DEEPSEEK_API_KEY": "runtime",
            "VISION_API_KEY": "runtime",
        })
        self.assertEqual(set(removed), {"token", "deepseek_api_key", "vision_api.api_key"})
        self.assertNotIn("token", config)
        self.assertNotIn("deepseek_api_key", config)
        self.assertEqual(config["vision_api"], {"model": "vision"})

    def test_systemd_unit_runs_bot_as_unprivileged_user(self):
        service = (Path(__file__).parents[2] / "deploy" / "qqbot.service").read_text(encoding="utf-8")
        self.assertIn("User=qqbot", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("QQBOT_CONFIG_PATH=/var/lib/qqbot/config.json", service)
        self.assertIn("QQBOT_PID_FILE=/run/qqbot/bot.pid", service)
        installer = (Path(__file__).parents[2] / "deploy" / "install-qqbot-service.sh").read_text(encoding="utf-8")
        self.assertIn('status --porcelain --untracked-files=all', installer)
        self.assertIn('find "${project_root}" -xdev', installer)
        self.assertIn('-path "${project_root}/data/*"', installer)
        self.assertIn('chown root:root "${file}"', installer)
        self.assertIn("QQBOT_SKIP_RESTART", installer)
