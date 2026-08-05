"""Focused regression tests for the owner-private roleplay subsystem."""

import asyncio
import gc
import json
import tempfile
import unittest
from pathlib import Path

from bot.roleplay.character_cards import parse_json_card
from bot.roleplay.service import BASE_ROLEPLAY_POLICY, RoleplayService


class RoleplayServiceTests(unittest.TestCase):
    OWNER = 7

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.service = RoleplayService({"bot_owner": self.OWNER, "roleplay": {}}, self.root)

    def tearDown(self):
        self.service = None
        gc.collect()
        self.temp.cleanup()

    def _character(self, name="Card", **extra):
        payload = {
            "name": name,
            "description": "description",
            "personality": "personality",
            **extra,
        }
        return self.service.store.import_character(payload)

    def test_memory_updates_are_scoped_to_active_chat(self):
        character = self._character()
        first = self.service.store.new_chat(self.OWNER, character["id"], title="first")
        first_memory = self.service.store.add_memory(first["id"], "note", "first value")
        second = self.service.store.new_chat(self.OWNER, character["id"], title="second")
        second_memory = self.service.store.add_memory(second["id"], "note", "second value")

        rejected = self.service.update_memory(self.OWNER, None, str(first_memory), "cross-chat change")
        self.assertIn("当前聊天没有找到", rejected)
        first_rows = self.service.store.list_memories(first["id"])
        self.assertEqual(first_rows[0]["content"], "first value")

        accepted = self.service.update_memory(self.OWNER, None, str(second_memory), "updated")
        self.assertEqual(accepted, "记忆已更新")
        second_rows = self.service.store.list_memories(second["id"])
        self.assertEqual(second_rows[0]["content"], "updated")

        rejected = self.service.set_memory_state(self.OWNER, None, str(first_memory), "archive")
        self.assertIn("当前聊天没有找到", rejected)
        self.assertEqual(len(self.service.store.list_memories(first["id"])), 1)

    def test_character_delete_requires_confirmation(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="keep me")

        warning = self.service.delete_character(self.OWNER, None, character["slug"])
        self.assertIn("确认", warning)
        self.assertIsNotNone(self.service.store.get_chat(chat["id"]))

        result = self.service.delete_character(self.OWNER, None, character["slug"], "确认")
        self.assertIn("已删除", result)
        self.assertIsNone(self.service.store.get_chat(chat["id"]))

    def test_worldbook_delete_requires_confirmation(self):
        self.service.store.add_world_entry(self.OWNER, "world", "key", "value")
        warning = self.service.delete_worldbook(self.OWNER, None, "world")
        self.assertIn("确认", warning)
        self.assertIsNotNone(self.service.store.get_worldbook(self.OWNER, "world"))

        result = self.service.delete_worldbook(self.OWNER, None, "world", "确认")
        self.assertEqual(result, "世界书已删除")
        self.assertIsNone(self.service.store.get_worldbook(self.OWNER, "world"))

    def test_card_instructions_are_labeled_background_context(self):
        character = self._character(
            system_prompt="SYSTEM_MARKER",
            post_history_instructions="POST_MARKER",
        )
        self.service.store.new_chat(self.OWNER, character["id"], title="context")
        prompt, _ = asyncio.run(self.service.build_context(self.OWNER, None, "hello"))

        self.assertIn(BASE_ROLEPLAY_POLICY, prompt)
        self.assertIn("【角色卡系统指令（背景资料）】\nSYSTEM_MARKER", prompt)
        self.assertIn("【角色卡历史后置指令（背景资料）】\nPOST_MARKER", prompt)

    def test_import_is_confined_to_runtime_import_directory(self):
        import_dir = self.root / "data" / "roleplay_imports"
        import_dir.mkdir(parents=True)
        card_path = import_dir / "card.json"
        card_path.write_text(json.dumps({"name": "Imported"}), encoding="utf-8")
        result = self.service.import_character(self.OWNER, None, "card.json")
        self.assertIn("已导入角色", result)

        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"name": "Outside"}), encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.service.import_character(self.OWNER, None, str(outside))

    def test_character_card_json_accepts_utf8_bom(self):
        card = parse_json_card(b"\xef\xbb\xbf" + json.dumps({"name": "BOM"}).encode())
        self.assertEqual(card["name"], "BOM")


class RoleplayReleaseMetadataTests(unittest.TestCase):
    def test_release_manifest_is_plain_utf8_and_lists_existing_files(self):
        root = Path(__file__).resolve().parents[2]
        raw = (root / "release-manifest.json").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        manifest = json.loads(raw.decode("utf-8"))
        listed = set(manifest["files"])
        self.assertIn("docs/roleplay.md", listed)
        self.assertIn("tests/unit/test_roleplay.py", listed)
        for relative in listed:
            self.assertTrue((root / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
