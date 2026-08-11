import asyncio
import unittest
from collections import deque
from unittest.mock import AsyncMock, Mock, patch

from bot.dispatcher import Dispatcher
from bot.media import describe_image_with_ocr
from bot.agent.tools.napcat import _bound_arguments


class UnifiedImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_url_skips_get_image_and_caches_vision(self):
        client = type("Client", (), {"call": AsyncMock()})()
        dispatcher = type("Dispatcher", (), {
            "config": {"runtime": {"media_timeout_seconds": 12}},
            "client": client,
        })()
        segment = {"type": "image", "data": {
            "file": "image-id", "url": "https://example.test/image.png",
            "sub_type": "0", "summary": "fallback",
        }}
        with patch("bot.ai.describe_image", new=AsyncMock(return_value="一张测试图片")) as describe:
            first = await describe_image_with_ocr(dispatcher, None, segment)
            second = await describe_image_with_ocr(dispatcher, None, segment)
        self.assertEqual(first, "图片：一张测试图片")
        self.assertEqual(second, "图片：一张测试图片")
        client.call.assert_not_awaited()
        describe.assert_awaited_once()
        self.assertEqual(describe.await_args.kwargs["image_url"],
                         "https://example.test/image.png")

    async def test_lookup_and_ocr_failure_falls_back_to_summary(self):
        client = type("Client", (), {
            "call": AsyncMock(return_value={"status": "timeout"}),
        })()
        dispatcher = type("Dispatcher", (), {
            "config": {"runtime": {
                "media_timeout_seconds": 12,
                "image_ocr_max_attempts": 2,
            }},
            "client": client,
        })()
        segment = {"type": "image", "data": {
            "file": "image-id", "sub_type": "0", "summary": "QQ摘要",
        }}
        with patch("bot.ai.describe_image", new=AsyncMock(return_value=None)):
            result = await describe_image_with_ocr(dispatcher, None, segment)
        self.assertEqual(result, "图片：QQ摘要")
        self.assertEqual(client.call.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in client.call.await_args_list],
            ["get_image", "ocr_image", "ocr_image_enhanced"],
        )

    async def test_vision_failure_runs_ocr_before_summary_fallback(self):
        client = type("Client", (), {
            "call": AsyncMock(return_value={
                "status": "ok", "data": {"text": "识别文字"},
            }),
        })()
        dispatcher = type("Dispatcher", (), {
            "config": {"runtime": {"image_ocr_max_attempts": 2}},
            "client": client,
        })()
        segment = {"type": "image", "data": {
            "url": "https://example.test/no-file.png",
            "sub_type": "0", "summary": "QQ摘要",
        }}
        with patch("bot.ai.describe_image", new=AsyncMock(return_value="[图片]")) as describe:
            result = await describe_image_with_ocr(dispatcher, 100, segment)
        self.assertEqual(result, "OCR文字：识别文字")
        describe.assert_awaited_once()
        self.assertEqual(client.call.await_count, 1)
        self.assertEqual(client.call.await_args.args, (
            "ocr_image", {"image": "https://example.test/no-file.png"}))

    async def test_group_context_uses_shared_media_path(self):
        dispatcher = Dispatcher.__new__(Dispatcher)
        message = [{"type": "image", "data": {"file": "image-id"}}]
        with patch(
                "bot.media.extract_message_context",
                new=AsyncMock(return_value="图片：统一结果")) as extract:
            result = await dispatcher._get_image_context(100, message)
        self.assertEqual(result, "图片：统一结果")
        extract.assert_awaited_once_with(dispatcher, 100, message)

    async def test_empty_url_without_summary_uses_generic_fallback(self):
        client = type("Client", (), {
            "call": AsyncMock(return_value={"status": "failed"}),
        })()
        dispatcher = type("Dispatcher", (), {
            "config": {"runtime": {"image_ocr_max_attempts": 2}},
            "client": client,
        })()
        segment = {"type": "image", "data": {
            "file": "image-id", "url": "", "sub_type": "0",
        }}
        with patch("bot.ai.describe_image", new=AsyncMock(return_value="[图片]")):
            result = await describe_image_with_ocr(dispatcher, None, segment)
        self.assertEqual(result, "图片：[图片]")

    async def test_sticker_skips_ocr_and_falls_back_to_summary(self):
        client = type("Client", (), {"call": AsyncMock()})()
        dispatcher = type("Dispatcher", (), {
            "config": {"runtime": {}}, "client": client,
        })()
        segment = {"type": "image", "data": {
            "url": "https://example.test/sticker.png",
            "sub_type": "1", "summary": "动画表情",
        }}
        with patch(
                "bot.ai.describe_image",
                new=AsyncMock(return_value="[表情/贴纸]")):
            result = await describe_image_with_ocr(dispatcher, None, segment)
        self.assertEqual(result, "图片：动画表情")
        client.call.assert_not_awaited()

    async def test_missing_ocr_image_argument_is_rejected_before_call(self):
        async def ocr_image(image):
            return image

        with self.assertRaisesRegex(ValueError, "image"):
            _bound_arguments(object(), ocr_image, {})


class OwnerPrivateDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rapid_owner_messages_are_merged_once(self):
        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher.config = {
            "bot_owner": 100,
            "runtime": {"owner_private_merge_seconds": 0.01},
        }
        dispatcher._owner_private_buffer = []
        dispatcher._owner_private_flush_task = None
        dispatcher._owner_private_buffer_lock = asyncio.Lock()
        dispatcher._owner_last_incoming_at = 0.0
        dispatcher._owner_recent_replies = deque(maxlen=5)
        dispatcher._background_tasks = set()
        dispatcher._max_background_tasks = 10
        companion = type("Companion", (), {
            "observe_owner_message": Mock(),
        })()
        handle = AsyncMock(return_value=True)
        dispatcher.agent_runtime = type("Runtime", (), {
            "companion": companion,
            "handle_event": handle,
        })()
        sender = {"nickname": "owner"}
        base_event = {
            "user_id": 100, "message_type": "private", "sender": sender,
        }
        await dispatcher._queue_owner_private_message(
            base_event, 100, [{"type": "text", "data": {"text": "你好"}}],
            "你好", sender, 1)
        await dispatcher._queue_owner_private_message(
            base_event, 100, [{"type": "text", "data": {"text": "在吗"}}],
            "在吗", sender, 2)
        await asyncio.sleep(0.05)
        handle.assert_awaited_once()
        merged_event = handle.await_args.args[1]
        self.assertEqual(merged_event["raw_message"], "你好\n在吗")
        self.assertEqual(len(merged_event["message"]), 2)

    def test_similar_owner_reply_is_suppressed_during_cooldown(self):
        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher.config = {"runtime": {
            "owner_reply_similarity_cooldown_seconds": 300,
        }}
        dispatcher._owner_recent_replies = deque(maxlen=5)
        dispatcher._record_owner_reply("有什么需要随时找我哦", now=100)
        self.assertTrue(dispatcher._owner_reply_is_repetitive(
            "有什么需要随时找我！", now=120))
        self.assertFalse(dispatcher._owner_reply_is_repetitive(
            "浙江今天可能还有阵雨", now=120))

    def test_template_owner_closing_is_suppressed_without_history(self):
        from bot.ai.reply_policy import should_suppress_reply

        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher.config = {"runtime": {}}
        dispatcher._owner_recent_replies = deque(maxlen=5)
        self.assertTrue(should_suppress_reply(
            dispatcher, 100, None, True, True, "有需要随时找我哦"))


if __name__ == "__main__":
    unittest.main()
