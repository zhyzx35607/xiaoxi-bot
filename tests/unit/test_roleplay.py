"""Focused regression tests for the owner-private roleplay subsystem."""

import asyncio
import gc
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.ai import runtime
from bot.ai.runtime import (
    _post_process_roleplay_reply,
    _roleplay_generation_profile,
    _split_roleplay_reply,
)
from bot.ai import providers
from bot.roleplay.character_cards import parse_json_card
from bot.roleplay.service import BASE_ROLEPLAY_POLICY, STORY_QUALITY_POLICY, RoleplayService


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

    def test_structured_roleplay_fields_redact_credentials(self):
        character = self._character(system_prompt="token=card-secret")
        self.service.store.new_chat(self.OWNER, character["id"], title="safe")
        prompt, _ = asyncio.run(self.service.build_context(self.OWNER, None, "hello"))
        self.assertNotIn("card-secret", prompt)
        self.assertIn("[已隐藏]", prompt)

    def test_long_roleplay_fields_keep_field_specific_lengths(self):
        description = "甲" * 3000 + " token=card-secret " + "乙" * 3000
        character = self._character(description=description)
        persona = self.service.store.create_persona(
            self.OWNER, "long", "人" * 4000, default=True)
        chat = self.service.store.new_chat(
            self.OWNER, character["id"], persona_id=persona["id"], title="long")
        self.service.store.add_message(chat["id"], "assistant", "话" * 6000)

        stored_character = self.service.store.get_character(character["id"])
        stored_persona = self.service.store.get_persona(self.OWNER, persona["id"])
        stored_message = self.service.store.recent_messages(chat["id"], 1)[0]
        self.assertGreater(len(stored_character["data"]["description"]), 5000)
        self.assertNotIn("card-secret", stored_character["data"]["description"])
        self.assertEqual(len(stored_persona["description"]), 4000)
        self.assertEqual(len(stored_message["content"]), 6000)

    def test_legacy_roleplay_context_and_exports_are_redacted(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="legacy")
        with self.service.store._connect() as connection:
            connection.execute(
                "INSERT INTO messages(chat_id,role,content,created_at) VALUES(?,?,?,?)",
                (chat["id"], "user", "Cookie: session=legacy-cookie", int(time.time())),
            )
            connection.execute(
                "INSERT INTO chat_summaries(chat_id,content,through_message_id,created_at) VALUES(?,?,?,?)",
                (chat["id"], "Authorization: Bearer legacy-bearer", 1, int(time.time())),
            )

        prompt, history = asyncio.run(self.service.build_context(self.OWNER, None, "hello"))
        exported = self.service.store.export_chat(chat["id"])
        serialized = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn("legacy-cookie", prompt)
        self.assertNotIn("legacy-bearer", prompt)
        self.assertNotIn("legacy-cookie", str(history))
        self.assertNotIn("legacy-cookie", serialized)
        self.assertNotIn("legacy-bearer", serialized)

    def test_story_mode_adds_long_form_quality_policy(self):
        character = self._character()
        self.service.store.new_chat(self.OWNER, character["id"], title="story")
        self.service.set_mode(self.OWNER, None, "story")

        prompt, _ = asyncio.run(self.service.build_context(self.OWNER, None, "continue"))

        self.assertIn(STORY_QUALITY_POLICY, prompt)
        self.assertIn("不受普通 QQ 群聊的短句", prompt)

    def test_roleplay_generation_profile_is_bounded(self):
        self.assertEqual(_roleplay_generation_profile({}), (1200, 0.82))
        self.assertEqual(
            _roleplay_generation_profile({}, story_mode=True),
            (2000, 0.82),
        )
        self.assertEqual(_roleplay_generation_profile({
            "roleplay": {"story_response_max_tokens": 1600},
        }, story_mode=True), (1600, 0.82))
        self.assertEqual(_roleplay_generation_profile({
            "roleplay": {"response_max_tokens": 9999, "response_temperature": -2},
        }), (2400, 0.1))
        self.assertEqual(_roleplay_generation_profile({
            "roleplay": {"response_max_tokens": "bad", "response_temperature": "bad"},
        }), (1200, 0.82))

    def test_roleplay_reply_preserves_narrative_and_splits_by_sentence(self):
        reply = "（她轻轻点头）" + "第一段。" * 180 + "\n\n" + "第二段继续。" * 80
        cleaned = _post_process_roleplay_reply(reply)
        parts = _split_roleplay_reply(cleaned, max_chars=500, max_parts=10)

        self.assertIn("（她轻轻点头）", cleaned)
        self.assertGreater(len(cleaned), 500)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 500 for part in parts))
        self.assertNotIn("已截断", "".join(parts))

    def test_context_history_has_total_and_per_message_budget(self):
        self.service.settings.update({
            "recent_message_limit": 20,
            "max_history_chars": 1000,
            "max_history_message_chars": 400,
        })
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="budget")
        for index in range(6):
            self.service.store.add_message(chat["id"], "user", f"message-{index}-" + "甲" * 600)

        _, history = asyncio.run(self.service.build_context(self.OWNER, None, "continue"))

        self.assertLessEqual(sum(len(item["content"]) for item in history), 1000)
        self.assertTrue(all(len(item["content"]) <= 400 for item in history))
        self.assertIn("message-5-", history[-1]["content"])
        indexes = [int(item["content"].split("message-", 1)[1].split("-", 1)[0]) for item in history]
        self.assertEqual(indexes, sorted(indexes))

    def test_record_exchange_runs_off_loop_and_persists(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="async")
        calls = []

        async def inline_to_thread(function, *args, **kwargs):
            calls.append(function.__name__)
            return function(*args, **kwargs)

        with patch("bot.roleplay.service.asyncio.to_thread", side_effect=inline_to_thread):
            asyncio.run(self.service.record_exchange(self.OWNER, None, "hello", "world"))

        self.assertEqual(calls, ["_record_exchange_sync"])
        rows = self.service.store.recent_messages(chat["id"], 10)
        self.assertEqual([(row["role"], row["content"]) for row in rows], [
            ("user", "hello"), ("assistant", "world"),
        ])

    def test_concurrent_exchanges_remain_complete_turns(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="serialized")

        async def run():
            await asyncio.gather(
                self.service.record_exchange(self.OWNER, None, "u1", "a1"),
                self.service.record_exchange(self.OWNER, None, "u2", "a2"),
            )

        asyncio.run(run())
        rows = self.service.store.recent_messages(chat["id"], 20)
        turns = [(row["role"], row["content"]) for row in rows]
        self.assertIn(turns, [
            [("user", "u1"), ("assistant", "a1"), ("user", "u2"), ("assistant", "a2")],
            [("user", "u2"), ("assistant", "a2"), ("user", "u1"), ("assistant", "a1")],
        ])

    def test_exchange_rolls_back_when_assistant_insert_fails(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="rollback")
        with self.service.store._connect() as connection:
            connection.executescript("""
                CREATE TRIGGER reject_assistant_message
                BEFORE INSERT ON messages
                WHEN NEW.role = 'assistant'
                BEGIN
                    SELECT RAISE(ABORT, 'assistant insert failed');
                END;
            """)

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store.record_exchange(chat["id"], "hello", "world")

        self.assertEqual(self.service.store.recent_messages(chat["id"], 10), [])
        with self.service.store._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM story_beats WHERE chat_id=?", (chat["id"],)
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE chat_id=?", (chat["id"],)
            ).fetchone()[0], 0)

    def test_sensitive_roleplay_memory_is_rejected_and_exchange_is_redacted(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="secret")

        rejected = self.service.add_memory(
            self.OWNER, None, "note", "token=super-secret-value")
        self.assertIn("不能把这段内容保存为记忆", rejected)
        self.assertEqual(self.service.store.list_memories(chat["id"]), [])

        asyncio.run(self.service.record_exchange(
            self.OWNER, None, "请记住 token=super-secret-value", "收到"))
        rows = self.service.store.recent_messages(chat["id"], 10)
        self.assertEqual(rows[0]["content"], "[敏感内容已省略]")
        self.assertNotIn("super-secret-value", str(rows))
        self.assertEqual(self.service.store.list_memories(chat["id"]), [])

    def test_exchange_uses_context_chat_snapshot_after_switch(self):
        character = self._character()
        first = self.service.store.new_chat(self.OWNER, character["id"], title="first")
        prompt, _, chat_id = asyncio.run(
            self.service.build_context_snapshot(self.OWNER, None, "hello"))
        self.assertTrue(prompt)
        self.assertEqual(chat_id, first["id"])

        second = self.service.store.new_chat(self.OWNER, character["id"], title="second")
        asyncio.run(self.service.record_exchange(
            self.OWNER, None, "hello", "reply", chat_id=first["id"]))
        self.assertEqual(
            [row["content"] for row in self.service.store.recent_messages(first["id"], 10)],
            ["hello", "reply"],
        )
        self.assertEqual(self.service.store.recent_messages(second["id"], 10), [])

    def test_sensitive_roleplay_message_skips_lightrag(self):
        character = self._character()
        self.service.store.new_chat(self.OWNER, character["id"], title="rag")
        self.service.lightrag.query = AsyncMock(return_value="should not be used")
        asyncio.run(self.service.build_context_snapshot(
            self.OWNER, None, "我的 cookie=private-value"))
        self.service.lightrag.query.assert_not_awaited()

    def test_storage_retention_keeps_newest_rows(self):
        character = self._character()
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="retention")
        for index in range(8):
            self.service.store.add_message(chat["id"], "user", str(index))
            self.service.store.add_story_beat(chat["id"], str(index))
            self.service.store.save_summary(chat["id"], str(index), index)
        self.service.store.audit(self.OWNER, "old", {}, chat["id"])
        with self.service.store._connect() as connection:
            connection.execute(
                "UPDATE audit_events SET created_at=? WHERE event_type='old'",
                (int(time.time()) - 10 * 86400,),
            )

        self.service.store.prune_retention(
            chat["id"], max_messages=3, max_story_beats=4,
            max_summaries=2, audit_retention_days=5)

        self.assertEqual([row["content"] for row in self.service.store.recent_messages(chat["id"], 20)], ["5", "6", "7"])
        self.assertEqual(len(self.service.store.recent_story_beats(chat["id"], 20)), 4)
        with self.service.store._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM chat_summaries WHERE chat_id=?", (chat["id"],)
            ).fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type='old'"
            ).fetchone()[0], 0)

    def test_story_mode_is_distinct_from_normal_roleplay(self):
        character = self._character()
        self.service.store.new_chat(self.OWNER, character["id"], title="mode")
        self.assertFalse(self.service.is_story_mode(self.OWNER, None))
        self.service.set_mode(self.OWNER, None, "story")
        self.assertTrue(self.service.is_story_mode(self.OWNER, None))

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


class RoleplayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_story_reply_is_preserved_split_and_persisted(self):
        owner = 7
        generated = "（她抬起眼睛）" + "这是一段连续叙事。" * 150
        sent = []
        recorded = []

        class Client:
            session = None

            async def send_private_msg(self, user_id, message):
                sent.append(message)
                return {"status": "ok"}

        class Roleplay:
            async def build_context(self, user_id, group_id, message):
                return "roleplay prompt", []

            async def is_story_mode_async(self, user_id, group_id):
                return True

            async def record_exchange(self, user_id, group_id, user_text, assistant_text):
                recorded.append((user_text, assistant_text))

        class Dispatcher:
            config = {
                "bot_owner": owner,
                "bot_qq": 8,
                "runtime": {},
                "sticker_mode": {},
                "roleplay": {"message_chunk_chars": 500, "max_message_segments": 10},
            }
            client = Client()
            roleplay = Roleplay()

        async def immediate_typing(dispatcher, user_id, awaitable):
            return await awaitable

        with patch.object(runtime, "_await_with_private_typing", side_effect=immediate_typing), \
                patch.object(runtime, "_call_deepseek", new=AsyncMock(return_value=generated)), \
                patch.object(runtime.asyncio, "sleep", new=AsyncMock()):
            result = await runtime.handle_ai_chat(
                Dispatcher(), None, owner, "继续", "主人", web_search_results="")

        texts = [segment[0]["data"]["text"] for segment in sent]
        self.assertTrue(result)
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 500 for text in texts))
        self.assertIn("（她抬起眼睛）", texts[0])
        self.assertEqual("".join(texts), generated)
        self.assertEqual(recorded, [("继续", generated)])


class RoleplayProviderPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_story_request_includes_bounded_max_tokens(self):
        payloads = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class Session:
            def post(self, url, **kwargs):
                payloads.append(kwargs["json"])
                return Response()

        config = {
            "sigmai_api_key": "test-key",
            "sigmai_base_url": "https://provider.invalid/v1",
            "sigmai_model": "story-model",
            "deepseek_api_key": "",
            "runtime": {"sigmai_timeout_seconds": 5},
        }
        providers._PROVIDER_COOLDOWNS.clear()
        result = await providers._call_deepseek_inner(
            config,
            [{"role": "user", "content": "continue"}],
            max_tokens=2000,
            temperature=0.82,
            session=Session(),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["max_tokens"], 2000)


if __name__ == "__main__":
    unittest.main()
