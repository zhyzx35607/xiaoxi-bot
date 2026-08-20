"""Engagement engine: judgment parsing, budget gates, delay mapping, flow."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

GROUP = 100
OWNER = 111
BOT = 222


class _Client:
    def __init__(self):
        self.session = None

    async def send_group_msg(self, group_id, message):
        return {"status": "ok"}


class _Dispatcher:
    def __init__(self):
        self.config = {
            "bot_owner": OWNER, "bot_qq": BOT,
            "engagement": {"enabled": True, "judge_max_per_hour": 3,
                           "reply_max_per_day": 2, "judge_every_messages": 8,
                           "judge_min_interval_seconds": 45},
            "groups": {str(GROUP): {"enabled": True}},
            "group_defaults": {},
        }
        self.client = _Client()
        self.tasks = []
        self._group_consecutive_replies = {}
        self.agent_runtime = None

    def create_background_task(self, coro, name=""):
        self.tasks.append(coro)
        coro.close()  # test stub: never awaited
        return None

    def _build_chat_context(self, group_id):
        return ""

    def _check_global_rate_limit(self):
        return True

    def _check_rate_limit(self, group_id):
        return True, 99

    def _record_ai_outcome(self, group_id, ok):
        pass

    def _record_rate_limit(self, group_id):
        pass

    def _record_global_rate_limit(self):
        pass


class ParseJudgmentTests(unittest.TestCase):
    def test_valid_json(self):
        from bot.services.engagement import parse_judgment
        result = parse_judgment('{"respond": true, "urgency": 0.9, "approach": "接梗",'
                                ' "reason": "有人在问问题", "delay_hint": "立刻", "topic": "游戏"}')
        self.assertTrue(result["respond"])
        self.assertEqual(0.9, result["urgency"])
        self.assertEqual("接梗", result["approach"])

    def test_malformed_degrades_to_none(self):
        from bot.services.engagement import parse_judgment
        self.assertIsNone(parse_judgment(""))
        self.assertIsNone(parse_judgment("我觉得可以说点什么"))
        self.assertIsNone(parse_judgment('{"respond": true, broken'))
        self.assertIsNone(parse_judgment('["respond"]'))

    def test_json_embedded_in_prose(self):
        from bot.services.engagement import parse_judgment
        result = parse_judgment('好的 {"respond": false, "reason": "灌水"} 完毕')
        self.assertFalse(result["respond"])

    def test_urgency_clamped(self):
        from bot.services.engagement import parse_judgment
        result = parse_judgment('{"respond": true, "urgency": 9}')
        self.assertEqual(1.0, result["urgency"])


class DelayMappingTests(unittest.TestCase):
    def test_urgent_is_fast(self):
        from bot.services.engagement import reply_delay_seconds
        fast = reply_delay_seconds({"delay_hint": "立刻", "urgency": 0.9})
        self.assertLessEqual(fast, 10.0)
        slow = reply_delay_seconds({"delay_hint": "不急", "urgency": 0.2})
        self.assertGreaterEqual(slow, 60.0)


class TrackerTests(unittest.TestCase):
    def test_question_signals(self):
        from bot.services.topic_tracker import TopicTracker
        tracker = TopicTracker()
        signals = tracker.record_message(GROUP, 1, "这个怎么搞啊", now=1000.0)
        self.assertIn("question_open", signals)
        # 闲聊不会误清除悬而未决的问题
        signals = tracker.record_message(GROUP, 2, "不知道", now=1030.0)
        self.assertNotIn("question_open", signals)
        self.assertIsNotNone(tracker.snapshot(GROUP)["open_question"])

    def test_unanswered_question(self):
        from bot.services.topic_tracker import TopicTracker
        tracker = TopicTracker()
        tracker.record_message(GROUP, 1, "有人会python吗", now=1000.0)
        # 闲聊不会清除悬而未决的问题
        signals = tracker.record_message(GROUP, 2, "今天天气不错", now=1005.0)
        self.assertNotIn("question_unanswered", signals)
        self.assertIsNotNone(tracker.snapshot(GROUP)["open_question"])
        # 超过 90s 无人应答 -> 信号
        signals = tracker.record_message(GROUP, 1, "顶一下", now=1100.0)
        self.assertIn("question_unanswered", signals)
        # 超过 300s -> 过期清除
        tracker.record_message(GROUP, 1, "算了", now=1400.0)
        self.assertIsNone(tracker.snapshot(GROUP)["open_question"])

    def test_emotion_spike(self):
        from bot.services.topic_tracker import TopicTracker
        tracker = TopicTracker()
        signals = []
        for i in range(3):
            signals = tracker.record_message(GROUP, i + 1, "哈哈哈哈", now=1000.0 + i * 10)
        self.assertIn("emotion_spike", signals)

    def test_streak_resets_on_bot_reply(self):
        from bot.services.topic_tracker import TopicTracker
        tracker = TopicTracker()
        tracker.record_message(GROUP, 1, "你好")
        tracker.record_message(GROUP, 2, "在吗")
        self.assertEqual(2, tracker.snapshot(GROUP)["streak_without_bot"])
        tracker.record_bot_spoke(GROUP)
        self.assertEqual(0, tracker.snapshot(GROUP)["streak_without_bot"])


class EngagementFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_triggers_judge_on_signal(self):
        from bot.services import engagement
        d = _Dispatcher()
        with patch.object(engagement, "_run_judgment", new=AsyncMock()) as judge:
            await engagement.on_group_message(d, GROUP, 1, "这个怎么弄？", "raw", "card", [], 1)
            await asyncio.sleep(0)
            self.assertTrue(judge.called)

    async def test_budget_blocks_excess_judgments(self):
        from bot.services import engagement
        d = _Dispatcher()
        with patch.object(engagement, "_run_judgment", new=AsyncMock()) as judge:
            for i in range(10):
                state = engagement._group_state(d, GROUP)
                state["pending_judge"] = False
                state["next_judge_after"] = 0
                await engagement.on_group_message(
                    d, GROUP, i + 10, "怎么搞？？", "raw", "card", [], i)
        self.assertEqual(3, judge.call_count)  # judge_max_per_hour=3

    async def test_disabled_config_is_noop(self):
        from bot.services import engagement
        d = _Dispatcher()
        d.config["engagement"]["enabled"] = False
        with patch.object(engagement, "_run_judgment", new=AsyncMock()) as judge:
            await engagement.on_group_message(d, GROUP, 1, "怎么搞？", "raw", "card", [], 1)
            self.assertFalse(judge.called)

    async def test_judgment_failure_silent_with_backoff(self):
        from bot.services import engagement
        d = _Dispatcher()
        state = engagement._group_state(d, GROUP)
        with patch.object(engagement, "_call_judge", new=AsyncMock(return_value=None)):
            await engagement._run_judgment(d, GROUP, 1, "raw", "card", [], 1, ["question_open"])
        self.assertFalse(state["pending_judge"])
        self.assertGreater(state["next_judge_after"], 0)
        self.assertEqual(2.0, state["judge_backoff"])
        self.assertEqual(0, len([t for t in d.tasks]))


if __name__ == "__main__":
    unittest.main()
