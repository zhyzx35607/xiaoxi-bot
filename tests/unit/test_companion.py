import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.agent.companion_runtime import CompanionRuntime
from bot.agent.runtime import AgentRuntime
from bot.agent.worker_service import AgentWorker
from bot.ai.providers import _call_deepseek_inner


class CompanionMemoryTests(unittest.TestCase):
    def test_birthday_is_permanent_and_recurring(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = CompanionRuntime({"bot_owner": 100}, Path(root) / "data" / "agent")
            runtime.observe_owner_message("我的生日是9.13")
            facts = runtime.store.list_facts(100, "生日")
            events = runtime.store.due_events(100, 9, 13)
            self.assertEqual(facts[0]["category"], "birthday")
            self.assertEqual(events[0]["recurrence"], "yearly")

            reopened = CompanionRuntime({"bot_owner": 100}, Path(root) / "data" / "agent")
            self.assertEqual(len(reopened.store.list_facts(100, "生日")), 1)

    def test_owner_reply_cancels_pending_followups(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = CompanionRuntime({"bot_owner": 100}, Path(root) / "data" / "agent")
            runtime.store.add_followup(100, "topic", {"seed": "hello"}, time.time() - 1)
            self.assertEqual(len(runtime.store.due_followups(100, time.time())), 1)
            runtime.observe_owner_message("我回来了")
            self.assertEqual(runtime.store.due_followups(100, time.time()), [])


class CompanionDecisionTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_forbids_invented_physical_experiences(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = CompanionRuntime({"bot_owner": 100}, Path(root) / "data" / "agent")
            prompt = runtime._build_prompt("idle", {}, time.time())
        self.assertIn("严禁声称自己刚吃饭、洗澡、喝茶、在食堂", prompt)
        self.assertIn("不能编造‘我这里也……’", prompt)

    def test_idle_checkin_waits_at_least_eight_hours(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = CompanionRuntime({"bot_owner": 100, "agent": {}}, Path(root) / "data" / "agent")
            now = time.time()
            state = runtime.state()
            state["last_interaction_at"] = now - 7 * 3600
            state["last_outgoing_at"] = now - 7 * 3600
            runtime.store.save_state(100, state)
            self.assertEqual(runtime._due_reason(now)[0], "none")
            self.assertEqual(runtime._due_reason(now + 2 * 3600)[0], "idle")

    async def test_decision_enqueues_message_and_advances_followup(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"bot_owner": 100, "agent": {"companion_min_gap_seconds": 300}}
            runtime = CompanionRuntime(config, Path(root) / "data" / "agent")
            now = time.time()
            runtime.store.add_followup(100, "unfinished", {"seed": "刚才的话题"}, now - 1)
            response = json.dumps({
                "should_send": True, "priority": "normal", "topic": "unfinished",
                "message_parts": ["主人，刚才那个话题我还惦记着。"],
                "emotion_delta": {"mood": "concerned", "concern": 0.7},
                "memory_candidates": [], "followup": {"enabled": True, "max_attempts": 4},
                "media_request": {},
            }, ensure_ascii=False)
            dispatcher = type("Dispatcher", (), {
                "config": config,
                "client": type("Client", (), {"session": None})(),
                "roleplay": None,
            })()
            with patch("bot.agent.companion_runtime._call_deepseek", new=AsyncMock(return_value=response)):
                result = await runtime.decide(dispatcher, now=now)
            self.assertEqual(result["reason"], "followup")
            self.assertEqual(len(runtime.store.due_outbox(100, now)), 1)
            self.assertEqual(runtime.store.due_followups(100, now), [])
            future = runtime.store.due_followups(100, now + 46 * 60)
            self.assertEqual(future[0]["attempt"], 1)
            self.assertEqual(runtime.state()["mood"], "concerned")

    async def test_normal_message_is_capped_to_one_short_part(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"bot_owner": 100, "agent": {"companion_min_gap_seconds": 21600}}
            runtime = CompanionRuntime(config, Path(root) / "data" / "agent")
            now = time.time()
            response = json.dumps({
                "should_send": True, "priority": "normal", "topic": "checkin",
                "message_parts": ["甲" * 180, "第二段"], "emotion_delta": {},
                "memory_candidates": [], "followup": {"enabled": False}, "media_request": {},
            }, ensure_ascii=False)
            dispatcher = type("Dispatcher", (), {
                "config": config, "client": type("Client", (), {"session": None})(),
                "roleplay": None,
            })()
            with patch("bot.agent.companion_runtime._call_deepseek", new=AsyncMock(return_value=response)):
                result = await runtime.decide(dispatcher, now=now, force=True)
            self.assertEqual(len(result["message_parts"]), 1)
            self.assertEqual(len(result["message_parts"][0]), 120)


class CompanionOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_sends_message_parts_in_order(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"bot_owner": 100, "agent": {"quiet_start": 23, "quiet_end": 9}}
            runtime = AgentRuntime(config, root)
            runtime.companion.store.enqueue(
                100, "hello", {"message_parts": ["第一段", "第二段"], "media_request": {}},
                time.time() - 1, "normal", "test:hello")

            class Client:
                def __init__(self):
                    self.sent = []
                    self.session = None

                async def send_private_msg(self, user_id, message):
                    self.sent.append((user_id, message))
                    return {"status": "ok"}

            dispatcher = type("Dispatcher", (), {
                "config": config, "agent_runtime": runtime, "client": Client(),
            })()
            with patch("bot.agent.worker_service.asyncio.sleep", new=AsyncMock()):
                delivered, failed = await AgentWorker(dispatcher)._deliver_companion_outbox()
            self.assertEqual((delivered, failed), (1, 0))
            self.assertEqual([item[1][0]["data"]["text"] for item in dispatcher.client.sent], ["第一段"])

    async def test_urgent_outbox_keeps_multiple_message_parts(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"bot_owner": 100, "agent": {"quiet_start": 23, "quiet_end": 9}}
            runtime = AgentRuntime(config, root)
            runtime.companion.store.enqueue(
                100, "urgent", {"message_parts": ["第一段", "第二段"], "media_request": {}},
                time.time() - 1, "urgent", "test:urgent")

            class Client:
                def __init__(self):
                    self.sent = []
                    self.session = None

                async def send_private_msg(self, user_id, message):
                    self.sent.append((user_id, message))
                    return {"status": "ok"}

            dispatcher = type("Dispatcher", (), {
                "config": config, "agent_runtime": runtime, "client": Client(),
            })()
            with patch("bot.agent.worker_service.asyncio.sleep", new=AsyncMock()):
                delivered, failed = await AgentWorker(dispatcher)._deliver_companion_outbox()
            self.assertEqual((delivered, failed), (1, 0))
            self.assertEqual(
                [item[1][0]["data"]["text"] for item in dispatcher.client.sent],
                ["第一段", "第二段"],
            )


class ProviderPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigmai_success_never_calls_deepseek(self):
        calls = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return {"choices": [{"message": {"content": "sigma ok"}, "finish_reason": "stop"}]}

            async def text(self):
                return ""

        class Session:
            def post(self, url, **kwargs):
                calls.append(url)
                return Response()

        config = {
            "sigmai_api_key": "sigma", "sigmai_base_url": "https://sigma.test/v1", "sigmai_model": "DeepSeek-V4-Flash",
            "deepseek_api_key": "deepseek", "deepseek_base_url": "https://deepseek.test", "deepseek_model": "deepseek-chat",
            "runtime": {"sigmai_timeout_seconds": 15, "deepseek_timeout_seconds": 20},
        }
        result = await _call_deepseek_inner(config, [{"role": "user", "content": "hi"}], session=Session())
        self.assertEqual(result, "sigma ok")
        self.assertEqual(calls, ["https://sigma.test/v1/chat/completions"])


if __name__ == "__main__":
    unittest.main()
