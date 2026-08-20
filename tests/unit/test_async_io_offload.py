"""事件循环阻塞 I/O 线程卸载与读-改-写竞态的回归测试。

覆盖：
- 表情包并发收集在锁内重读合并，不丢条目（bot/ai/stickers.py）
- 群文件上传记录并发写不丢条目（bot/events/notice.py）
- companion decide() 跨 await 的状态更新合并（bot/agent/companion_runtime.py）
- 安全审计日志异常收窄（bot/security/core.py）
- AI 记忆读改写配对与压缩调度外移（bot/ai/memory.py）
- Dispatcher 运行状态异步保存（bot/dispatcher.py）
- 角色扮演导出原子写且格式不变（bot/roleplay/service.py）
- B 站视频流式下载写盘卸载（bot/integrations/bilibili.py）
"""

import asyncio
import gc
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.ai import memory as memory_module
from bot.ai import stickers as stickers_module
from bot.ai.memory import persist_memory_entries, schedule_pending_memory_compression
from bot.agent.companion_runtime import CompanionRuntime
from bot.events import notice as notice_module
from bot.security import core as security_core


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class StickerCollectRaceTests(unittest.IsolatedAsyncioTestCase):
    """并发 collect_sticker_async 不得基于同一旧快照互相覆盖。"""

    def _dispatcher(self):
        class Client:
            session = None

            async def call(self, action, params, **kwargs):
                return {"status": "ok", "data": {"url": "http://img.invalid/s.jpg"}}

        return type("Dispatcher", (), {
            "config": {"sticker_mode": {"collect": True, "max_stickers": 50}},
            "client": Client(),
        })()

    async def test_concurrent_collects_keep_both_entries(self):
        barrier = asyncio.Event()
        calls = []

        async def fake_analyze(config, image_url, session=None):
            calls.append(image_url)
            if len(calls) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=5)
            return "描述|开心|关键词|场景"

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stickers_module, "STICKER_DIR", directory), \
                patch.object(stickers_module, "_analyze_sticker_vision", new=fake_analyze):
            await asyncio.gather(
                stickers_module.collect_sticker_async(
                    self._dispatcher(), 12345, "file_a", "1", is_private=True),
                stickers_module.collect_sticker_async(
                    self._dispatcher(), 12345, "file_b", "1", is_private=True),
            )
            path = os.path.join(directory, "private_12345.json")
            rows = await asyncio.to_thread(_read_json, path)
        self.assertEqual({row["file"] for row in rows}, {"file_a", "file_b"})


class GroupUploadRaceTests(unittest.IsolatedAsyncioTestCase):
    """并发 group_upload 记录不得丢条目。"""

    async def test_concurrent_upload_records_are_all_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "group_files.json")
            with patch.object(notice_module, "_GROUP_FILES_PATH", path):
                await asyncio.gather(*(
                    asyncio.to_thread(
                        notice_module._record_group_upload, 100,
                        {"ts": float(i), "user_id": 1000 + i, "name": "f{}.zip".format(i),
                         "file_id": "id{}".format(i), "busid": "", "size": i})
                    for i in range(20)
                ))
            data = await asyncio.to_thread(_read_json, path)
        self.assertEqual(len(data["100"]), 20)
        self.assertEqual(
            {entry["file_id"] for entry in data["100"]},
            {"id{}".format(i) for i in range(20)})


class CompanionDecideMergeTests(unittest.IsolatedAsyncioTestCase):
    """decide() 的状态保存必须合并 AI 调用期间 observe_owner_message 的更新。"""

    async def test_decide_merges_concurrent_observe_update(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"bot_owner": 100, "agent": {"companion_min_gap_seconds": 300}}
            runtime = CompanionRuntime(config, Path(root) / "data" / "agent")
            now = time.time()
            response = json.dumps({
                "should_send": True, "priority": "normal", "topic": "checkin",
                "message_parts": ["最近怎么样？"],
                "emotion_delta": {"mood": "concerned", "concern": 0.7},
                "memory_candidates": [], "followup": {"enabled": False},
                "media_request": {},
            }, ensure_ascii=False)

            async def ai_call(*args, **kwargs):
                # 模拟 AI 调用期间主人来消息，observe 在另一线程完成更新
                runtime.observe_owner_message("我回来了", timestamp=now)
                return response

            dispatcher = type("Dispatcher", (), {
                "config": config,
                "client": type("Client", (), {"session": None})(),
                "roleplay": None,
            })()
            with patch("bot.agent.companion_runtime._call_deepseek",
                       new=AsyncMock(side_effect=ai_call)):
                result = await runtime.decide(dispatcher, now=now, force=True)
            self.assertIsNotNone(result)
            state = runtime.state()
            # decide 的情绪 delta 生效
            self.assertEqual(state["mood"], "concerned")
            self.assertEqual(state["last_decision_at"], now)
            # observe 在 AI 调用期间写入的更新不被旧快照覆盖
            self.assertEqual(state["last_interaction_at"], now)


class SecurityAuditLogTests(unittest.TestCase):
    def test_permission_error_logs_warning_not_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "security_events.json")
            with patch.object(security_core, "_LOG_PATH", path), \
                    patch("builtins.open", side_effect=PermissionError("denied")):
                with self.assertLogs("qqbot", level="WARNING") as captured:
                    result = security_core.load_security_events()
        self.assertEqual(result, [])
        self.assertTrue(any("Security event log read failed" in line
                            for line in captured.output))

    def test_missing_and_corrupt_log_read_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "security_events.json")
            with patch.object(security_core, "_LOG_PATH", path):
                self.assertEqual(security_core.load_security_events(), [])
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{broken json")
                self.assertEqual(security_core.load_security_events(), [])

    def test_record_event_roundtrip_still_works(self):
        dispatcher = type("Dispatcher", (), {"config": {}})()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "security_events.json")
            with patch.object(security_core, "_LOG_PATH", path):
                security_core.record_security_event(dispatcher, "url", 1, 2, "detail")
                rows = security_core.load_security_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "url")


class MemoryPersistencePairingTests(unittest.IsolatedAsyncioTestCase):
    """persist_memory_entries 在同一线程任务内完成读-改-写。"""

    async def test_concurrent_user_memory_writes_keep_both(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "MEMORY_DIR", directory):
            await asyncio.gather(
                asyncio.to_thread(persist_memory_entries, 424242, 313131,
                                  [{"role": "user", "content": "甲"}], None, None),
                asyncio.to_thread(persist_memory_entries, 424242, 313131,
                                  [{"role": "user", "content": "乙"}], None, None),
            )
            path = memory_module._user_memory_file(424242, 313131)
            rows = await asyncio.to_thread(_read_json, path)
        self.assertEqual({row["content"] for row in rows}, {"甲", "乙"})

    async def test_save_memory_defers_compression_to_event_loop(self):
        entries = [{"role": "user", "content": "m{}".format(i)} for i in range(25)]
        config = {"runtime": {"enable_long_memory_compress": True}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "MEMORY_DIR", directory):
            try:
                pending = memory_module._save_memory(555001, entries, config, session=object())
                self.assertIsNotNone(pending)
                key, coro = pending
                self.assertEqual(key, "group:555001")
                coro.close()  # 本用例不调度，避免未 await 警告
                # 无线程/无 loop 上下文时不得遗留未调度的协程
                schedule_pending_memory_compression(None)
            finally:
                memory_module._memories.pop(555001, None)
                memory_module._memory_timestamps.pop(555001, None)

    async def test_scheduled_pending_compression_appends_long_memory(self):
        entries = [{"role": "user", "content": "m{}".format(i)} for i in range(25)]
        config = {"runtime": {"enable_long_memory_compress": True}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "MEMORY_DIR", directory):
            try:
                pending = memory_module._save_memory(555002, list(entries), config, session=object())
                self.assertIsNotNone(pending)
                with patch("bot.ai.memory._call_deepseek",
                           new=AsyncMock(return_value="这是对话摘要内容")):
                    schedule_pending_memory_compression(pending)
                    task = memory_module._LONG_MEMORY_TASKS.get("group:555002")
                    self.assertIsNotNone(task)
                    await task
                path = memory_module._long_memory_file(555002)
                rows = await asyncio.to_thread(_read_json, path)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["content"], "这是对话摘要内容")
            finally:
                memory_module._memories.pop(555002, None)
                memory_module._memory_timestamps.pop(555002, None)


class DispatcherAsyncSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_runtime_state_async_writes_snapshot(self):
        from bot.dispatcher import Dispatcher

        client = type("Client", (), {"session": None})()
        dispatcher = Dispatcher({"runtime": {}}, client)
        with tempfile.TemporaryDirectory() as directory:
            dispatcher._state_path = os.path.join(directory, "runtime_state.json")
            dispatcher._state_dirty = True
            await dispatcher.save_runtime_state_async()
            state = await asyncio.to_thread(_read_json, dispatcher._state_path)
        self.assertIn("group_msg_counts", state)
        self.assertIn("daily_likes", state)
        self.assertFalse(dispatcher._state_dirty)

    async def test_save_runtime_state_async_respects_dirty_gate(self):
        from bot.dispatcher import Dispatcher

        client = type("Client", (), {"session": None})()
        dispatcher = Dispatcher({"runtime": {}}, client)
        with tempfile.TemporaryDirectory() as directory:
            dispatcher._state_path = os.path.join(directory, "runtime_state.json")
            dispatcher._state_dirty = False
            await dispatcher.save_runtime_state_async()
            self.assertFalse(os.path.exists(dispatcher._state_path))


class RoleplayExportAtomicTests(unittest.TestCase):
    OWNER = 7

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        from bot.roleplay.service import RoleplayService
        self.service = RoleplayService({"bot_owner": self.OWNER, "roleplay": {}}, self.root)

    def tearDown(self):
        self.service = None
        gc.collect()
        self.temp.cleanup()

    def test_export_character_writes_same_format_atomically(self):
        character = self.service.store.import_character(
            {"name": "Card", "description": "d", "personality": "p"})
        result = self.service.export_character(self.OWNER, None, character["slug"])
        self.assertTrue(result.startswith("已导出"))
        target = self.root / "data" / "roleplay_exports" / "character-{}.json".format(character["slug"])
        # 与原子写前一致的格式：indent=2、ensure_ascii=False、无尾部换行
        expected = json.dumps(
            {"spec": "chara_card_v2", "spec_version": "2.0", "data": character["data"]},
            ensure_ascii=False, indent=2)
        self.assertEqual(target.read_text(encoding="utf-8"), expected)
        # 原子写不遗留临时文件
        leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_export_chat_writes_parseable_json_atomically(self):
        character = self.service.store.import_character(
            {"name": "Card", "description": "d", "personality": "p"})
        chat = self.service.store.new_chat(self.OWNER, character["id"], title="t")
        result = self.service.export_chat(self.OWNER, None)
        self.assertTrue(result.startswith("已导出"))
        target = self.root / "data" / "roleplay_exports" / "chat-{}.json".format(chat["id"])
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertTrue(payload)
        leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


class BilibiliDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_mp4_streams_chunks_to_disk(self):
        from bot.integrations import bilibili

        chunks = [b"x" * 1000, b"y" * 500]

        class Content:
            def iter_chunked(self, size):
                async def gen():
                    for chunk in chunks:
                        yield chunk
                return gen()

        class Response:
            status = 200
            headers = {}
            content = Content()

        class GetContext:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *args):
                return False

        class Session:
            def get(self, url, headers=None, timeout=None):
                return GetContext()

        dispatcher = type("Dispatcher", (), {
            "client": type("Client", (), {"session": Session()})(),
        })()
        path = await bilibili.download_mp4(dispatcher, "http://cdn.invalid/v.mp4", "BV1test", 10000)
        try:
            self.assertTrue(path)
            content = await asyncio.to_thread(_read_bytes, path)
            self.assertEqual(content, b"x" * 1000 + b"y" * 500)
        finally:
            if path and os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
