"""Deployment helper regression tests."""

import importlib.util
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

from app.logging_setup import RedactingFormatter, sanitize_log_message


ROOT = Path(__file__).resolve().parents[2]


def load_deploy_module(name):
    path = ROOT / "deploy" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NapCatLogFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_deploy_module("napcat_log_filter.py")

    def test_drops_message_and_noisy_info_lines(self):
        self.assertFalse(self.module.should_emit("[info] received group message"))
        self.assertFalse(self.module.should_emit("131 js loaded"))
        self.assertTrue(self.module.should_emit("[warn] reconnect failed"))
        self.assertTrue(self.module.should_emit("libprotobuf ERROR invalid UTF-8"))
        self.assertTrue(self.module.should_emit("NapCat WebSocket server started"))

    def test_redacts_credentials_and_long_identifiers(self):
        line = 'ERROR token=secret access_token="other" group=123456789 user=987654321'
        sanitized = self.module.sanitize_line(line)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("other", sanitized)
        self.assertNotIn("123456789", sanitized)
        self.assertNotIn("987654321", sanitized)
        self.assertGreaterEqual(sanitized.count("<redacted>"), 2)
        self.assertGreaterEqual(sanitized.count("<id>"), 2)

    def test_redacts_structured_payload_from_warning(self):
        line = 'WARNING Unknown JSON event {"message":"private text","token":"secret"}'
        sanitized = self.module.sanitize_line(line)
        self.assertEqual(sanitized, "WARNING Unknown JSON event { <payload redacted>")
        self.assertNotIn("private text", sanitized)
        self.assertNotIn("secret", sanitized)


class ApplicationLogRedactionTests(unittest.TestCase):
    def test_redacts_credentials_ids_and_payloads(self):
        sanitized = sanitize_log_message(
            'user=123456789 token="secret" text=private conversation'
        )

        self.assertNotIn("123456789", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("private conversation", sanitized)
        self.assertIn("<id>", sanitized)
        self.assertIn("<payload redacted>", sanitized)

    def test_redacts_authorization_and_cookie_headers(self):
        sanitized = sanitize_log_message(
            "Authorization: Bearer secret-token, Cookie: session=private-value"
        )

        self.assertNotIn("secret-token", sanitized)
        self.assertNotIn("private-value", sanitized)
        self.assertEqual(sanitized.count("<redacted>"), 2)

    def test_redacts_query_credentials_and_common_api_key_names(self):
        sanitized = sanitize_log_message(
            "https://example.invalid/callback?access_token=secret-a&x-api-key=secret-b#sessdata=secret-c "
            "client-key=secret-d SESSDATA=secret-e"
        )

        for secret in ("secret-a", "secret-b", "secret-c", "secret-d", "secret-e"):
            self.assertNotIn(secret, sanitized)
        self.assertGreaterEqual(sanitized.count("<redacted>"), 5)

    def test_formatter_redacts_exception_text(self):
        formatter = RedactingFormatter("%(levelname)s %(message)s")
        try:
            raise RuntimeError("access_token=secret-token user=123456789")
        except RuntimeError:
            record = logging.LogRecord(
                "qqbot", 40, __file__, 1, "request failed", (), sys.exc_info()
            )

        formatted = formatter.format(record)
        self.assertNotIn("secret-token", formatted)
        self.assertNotIn("123456789", formatted)


class NapCatConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_deploy_module("configure_napcat_logging.py")

    def test_logging_config_is_hardened_without_losing_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "napcat.json"
            path.write_text(json.dumps({
                "consoleLog": True,
                "consoleLogLevel": "debug",
                "fileLog": True,
                "fileLogLevel": "debug",
                "packetBackend": "example",
            }), encoding="utf-8")
            os.chmod(path, 0o600)

            self.assertTrue(self.module.update_config(path))
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(updated["consoleLog"])
            self.assertFalse(updated["fileLog"])
            self.assertEqual(updated["consoleLogLevel"], "warn")
            self.assertEqual(updated["fileLogLevel"], "warn")
            self.assertEqual(updated["packetBackend"], "example")
            if os.name != "nt":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertFalse(self.module.update_config(path))

    def test_correct_logging_settings_still_harden_insecure_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "onebot11_123456.json"
            path.write_text(json.dumps({
                "consoleLog": False,
                "consoleLogLevel": "warn",
                "fileLog": False,
                "fileLogLevel": "warn",
            }), encoding="utf-8")
            os.chmod(path, 0o644)

            updated = self.module.update_config(path)
            if os.name != "nt":
                self.assertTrue(updated)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            else:
                self.assertFalse(updated)
            self.assertFalse(self.module.update_config(path))


class BackupRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_deploy_module("prune_qqbot_backups.py")

    def test_keeps_newest_floor_and_only_selects_expired_regular_files(self):
        now = 2_000_000_000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for index, age_days in enumerate((1, 2, 40, 50)):
                path = root / "backup-{}.tar.gz".format(index)
                path.write_text("backup", encoding="utf-8")
                timestamp = now - age_days * 86400
                os.utime(path, (timestamp, timestamp))
                files.append(path)
            (root / "nested").mkdir()

            selected = self.module.select_for_pruning(
                root, keep=2, max_age_days=30, now=now
            )

            self.assertEqual(selected, [files[2], files[3]])


class ServiceInstallScriptTests(unittest.TestCase):
    def test_config_migration_stops_service_and_preserves_runtime_owner(self):
        script = (ROOT / "deploy" / "install-qqbot-service.sh").read_text(encoding="utf-8")

        self.assertIn("systemctl stop qqbot.service", script)
        self.assertIn("trap restore_runtime_services EXIT", script)
        self.assertIn("--owner-user qqbot", script)
        self.assertIn("--owner-group qqbot", script)
        self.assertIn("systemctl disable --now napcat-login-watchdog.timer", script)
        self.assertIn("systemctl enable --now napcat-login-watchdog.service", script)
        permission_walk = script.index("while IFS= read -r -d '' directory")
        self.assertLess(script.index("systemctl stop napcat-login-watchdog.timer"), permission_walk)
        self.assertLess(script.index("systemctl stop napcat-login-watchdog.service"), permission_walk)
        self.assertLess(script.index("systemctl stop qqbot.service"), permission_walk)
        self.assertLess(
            script.index("systemctl enable --now napcat-login-watchdog.service"),
            script.index("systemctl disable --now napcat-login-watchdog.timer"),
        )

    def test_napcat_install_hardens_account_config_permissions(self):
        script = (ROOT / "deploy" / "install-napcat-service.sh").read_text(encoding="utf-8")
        self.assertIn("-name 'onebot11_*.json'", script)
        self.assertIn('chmod 0600 "${config_file}"', script)

    def test_journald_has_bounded_retention(self):
        config = (ROOT / "deploy" / "qqbot-journald.conf").read_text(encoding="utf-8")
        self.assertIn("SystemMaxUse=192M", config)
        self.assertIn("RuntimeMaxUse=64M", config)
        self.assertIn("MaxRetentionSec=7day", config)
        script = (ROOT / "deploy" / "install-qqbot-service.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/systemd/journald.conf.d/30-qqbot.conf", script)


if __name__ == "__main__":
    unittest.main()
