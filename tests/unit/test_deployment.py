"""Deployment helper regression tests."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
