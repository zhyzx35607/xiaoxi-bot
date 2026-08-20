"""State-machine unit tests for the ACG delivery checkpoint/recovery flow."""

import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from bot.services import scheduler


class _Client:
    """OneBot client stub; records forwards and serves scripted history."""

    def __init__(self, send_results=None, history_pages=None, connected=True):
        self.is_connected = connected
        self.sent = []
        self.history_calls = 0
        self._send_results = list(send_results or [])
        self._history_pages = list(history_pages or [])

    async def send_group_forward_msg(self, group_id, nodes):
        self.sent.append((group_id, nodes))
        if self._send_results:
            return self._send_results.pop(0)
        return {"status": "ok", "retcode": 0}

    async def get_group_msg_history(self, group_id, count=50):
        self.history_calls += 1
        if self._history_pages:
            return self._history_pages.pop(0)
        return {"messages": []}

    async def send_private_msg(self, user_id, message):
        return {"status": "ok"}


class _Stub:
    def __init__(self, client, acg_cfg=None, groups=None):
        self.config = {
            "bot_owner": 9,
            "bot_qq": 1,
            "acg_images": acg_cfg if acg_cfg is not None else {"enabled": True},
            "groups": groups if groups is not None else {
                "100": {"enabled": True, "features": {"acg_images": True}},
            },
        }
        self.client = client


def _pool_urls(count, prefix="https://img.example.com/p"):
    return ["{}{}.jpg".format(prefix, index) for index in range(count)]


class AcgDeliveryStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "acg.json")
        patcher = patch.object(scheduler, "_ACG_HISTORY_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = patch.object(scheduler.asyncio, "sleep", new=AsyncMock())
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self.sleep_mock = sleep_patcher.new

    def _seed_state(self, **overrides):
        state = scheduler._new_acg_state()
        state.update(overrides)
        scheduler._save_acg_state(state)
        return state

    def _seed_delivery(self, batch_id="test-batch", urls=None, groups=None,
                       attempts=None):
        state = scheduler._new_acg_state()
        state["pending_due"] = True
        state["delivery"] = {
            "batch_id": batch_id,
            "urls": urls if urls is not None else ["https://img.example.com/a.jpg"],
            "remaining_groups": groups if groups is not None else ["100"],
            "attempts": attempts if attempts is not None else {},
            "created_at": time.time(),
            "next_retry_at": 0,
        }
        scheduler._save_acg_state(state)
        return state

    async def test_full_pool_is_sent_as_forward_and_state_cleared(self):
        urls = _pool_urls(20)
        self._seed_state(pending_due=True, pool=list(urls))
        client = _Client()
        stub = _Stub(client)

        result = await scheduler._try_send_acg_delivery(stub)

        self.assertTrue(result)
        # 默认 images_per_forward=10，20 张分 2 条合并转发
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(client.sent[0][0], 100)
        first_nodes = client.sent[0][1]
        self.assertEqual(len(first_nodes), 11)  # 1 header + 10 images
        self.assertEqual(first_nodes[0]["type"], "node")
        self.assertIn("批次 #", first_nodes[0]["data"]["content"])
        image_nodes = [node for node in first_nodes[1:]]
        self.assertTrue(all(
            node["data"]["content"][0]["type"] == "image" for node in image_nodes))
        self.assertEqual(
            [node["data"]["content"][0]["data"]["file"] for node in image_nodes],
            urls[:10])
        state = scheduler._load_acg_state()
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["pending_due"])
        self.assertEqual(state["pool"], [])
        # 已发送 URL 进入 recent，供 7 天去重
        self.assertEqual(sorted(state["recent"]), sorted(urls))

    async def test_unconfirmed_send_recovers_via_history_without_resend(self):
        self._seed_delivery(batch_id="test-batch")
        client = _Client(
            send_results=[{"status": "timeout", "retcode": -1}],
            # 第一次轮询未出现，第二次轮询命中批次标记
            history_pages=[{"messages": []},
                           {"messages": [{"raw_message": "forward 批次 #test-batch-1"}]}],
        )
        stub = _Stub(client, acg_cfg={
            "enabled": True,
            "timeout_confirm_checks": 3,
            "timeout_confirm_interval_seconds": 20,
        })

        result = await scheduler._try_send_acg_delivery(stub)

        self.assertTrue(result)
        # 只发送一次，历史确认后不再重复推送
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.history_calls, 2)
        state = scheduler._load_acg_state()
        self.assertIsNone(state["delivery"])
        self.assertFalse(state["pending_due"])
        self.assertEqual(state.get("last_failure"), None)

    async def test_unconfirmed_send_exhausting_polls_is_treated_as_unsent(self):
        self._seed_delivery(batch_id="test-batch")
        client = _Client(
            send_results=[{"status": "timeout", "retcode": -1}],
            history_pages=[{"messages": []}] * 3,
        )
        stub = _Stub(client, acg_cfg={
            "enabled": True,
            "timeout_confirm_checks": 3,
            "timeout_confirm_interval_seconds": 20,
            "max_delivery_attempts": 3,
            "retry_base_seconds": 30,
        })

        before = time.time()
        result = await scheduler._try_send_acg_delivery(stub)

        self.assertFalse(result)
        self.assertEqual(len(client.sent), 1)
        # 3 次轮询全部未命中
        self.assertEqual(client.history_calls, 3)
        state = scheduler._load_acg_state()
        delivery = state["delivery"]
        self.assertIsNotNone(delivery)
        # 按未发送处理：保留待重试群并安排退避重试
        self.assertEqual(delivery["remaining_groups"], ["100"])
        self.assertEqual(delivery["attempts"], {"100": 1})
        self.assertGreaterEqual(delivery["next_retry_at"], before + 30)
        self.assertTrue(state["pending_due"])

    async def test_offline_websocket_skips_delivery_round(self):
        self._seed_delivery(batch_id="test-batch")
        client = _Client(connected=False)
        stub = _Stub(client)

        result = await scheduler._try_send_acg_delivery(stub)

        self.assertFalse(result)
        self.assertEqual(client.sent, [])
        self.assertEqual(client.history_calls, 0)
        # 离线只是跳过本轮，不推进投递状态
        state = scheduler._load_acg_state()
        self.assertIsNotNone(state["delivery"])
        self.assertEqual(state["delivery"]["remaining_groups"], ["100"])
        self.assertEqual(state["delivery"]["attempts"], {})

    async def test_attempted_group_is_recovered_from_history_not_resent(self):
        # 上次进程在 OneBot 接受转发后、落盘前停止：attempts>0 且历史已有批次
        self._seed_delivery(batch_id="test-batch", attempts={"100": 1})
        client = _Client(
            history_pages=[{"messages": [{"raw_message": "批次 #test-batch-1"}]}],
        )
        stub = _Stub(client)

        result = await scheduler._try_send_acg_delivery(stub)

        self.assertTrue(result)
        self.assertEqual(client.sent, [])
        self.assertEqual(client.history_calls, 1)
        state = scheduler._load_acg_state()
        self.assertIsNone(state["delivery"])


class AcgRecentDedupeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "acg.json")
        patcher = patch.object(scheduler, "_ACG_HISTORY_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_recent_url_within_seven_days_is_not_collected_again(self):
        now = time.time()
        state = scheduler._new_acg_state()
        state["recent"] = {"https://img.example.com/dup.jpg": now - 6 * 86400}
        scheduler._save_acg_state(state)
        image = type("Image", (), {"url": "https://img.example.com/dup.jpg"})()
        resolver = AsyncMock(return_value=image)
        stub = _Stub(_Client())

        with patch("bot.integrations.mukyu.fetch_random_image", resolver):
            collected = await scheduler._collect_one_acg_image(stub)

        self.assertFalse(collected)
        resolver.assert_awaited_once()
        state = scheduler._load_acg_state()
        self.assertEqual(state["pool"], [])

    async def test_recent_url_older_than_seven_days_can_be_collected_again(self):
        now = time.time()
        state = scheduler._new_acg_state()
        state["recent"] = {"https://img.example.com/old.jpg": now - 8 * 86400}
        scheduler._save_acg_state(state)
        image = type("Image", (), {"url": "https://img.example.com/old.jpg"})()
        resolver = AsyncMock(return_value=image)
        stub = _Stub(_Client())

        with patch("bot.integrations.mukyu.fetch_random_image", resolver):
            collected = await scheduler._collect_one_acg_image(stub)

        self.assertTrue(collected)
        state = scheduler._load_acg_state()
        self.assertEqual(state["pool"], ["https://img.example.com/old.jpg"])


if __name__ == "__main__":
    unittest.main()
