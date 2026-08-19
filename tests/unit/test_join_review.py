"""入群申请群内审批流：公告、确定性同意/拒绝匹配、/审批 开关。"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot.permission as permission_module
from bot.events import request as request_module


class FakeClient:
    def __init__(self, roles=None):
        self.roles = roles or {}
        self.calls = []
        self.sent = []

    async def get_group_member_info(self, group_id, user_id):
        return {"status": "ok", "data": {"role": self.roles.get(user_id, "member")}}

    async def send_group_msg(self, group_id, text):
        self.sent.append((group_id, text))
        return {"status": "ok", "data": {"message_id": 12345}}

    async def set_group_add_request(self, flag, sub_type, approve=True, reason=""):
        self.calls.append(("set_group_add_request", flag, sub_type, approve, reason))
        return {"status": "ok"}


def make_config(join_review=True):
    return {
        "bot_owner": 999,
        "bot_qq": 888,
        "groups": {"300": {"enabled": True, "join_review": join_review}},
    }


def make_dispatcher(config, client):
    return type("D", (), {"config": config, "client": client})()


def request_event(**overrides):
    event = {
        "post_type": "request", "request_type": "group", "sub_type": "add",
        "group_id": 300, "user_id": 202, "flag": "flag-abc",
        "comment": "答案是小汐", "time": 12345,
    }
    event.update(overrides)
    return event


def review_message_event(text, *, user_id=201, role="admin", reply_id=None):
    segments = []
    if reply_id is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "message_type": "group", "group_id": 300, "user_id": user_id,
        "sender": {"role": role}, "message": segments, "raw_message": text,
    }


def seed_pending(path, entries):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def pending_entry(**overrides):
    entry = {
        "ts": time.time(), "request_type": "group", "sub_type": "add",
        "group_id": 300, "user_id": 202, "comment": "答案是小汐",
        "flag": "flag-abc", "announce_message_id": 12345,
        "announce_expires_at": time.time() + 600,
    }
    entry.update(overrides)
    return entry


class JoinReviewBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permission_module._bot_role_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.pending_path = str(Path(self._tmp.name) / "pending.json")
        self._patches = [
            patch.object(request_module, "_PENDING_PATH", self.pending_path),
            patch.object(request_module, "is_blacklisted", lambda g, u: False),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()
        self._tmp.cleanup()

    def load_pending(self):
        return json.loads(Path(self.pending_path).read_text(encoding="utf-8"))


class JoinReviewAnnounceTests(JoinReviewBase):
    async def test_announce_sent_when_bot_is_admin(self):
        client = FakeClient(roles={888: "admin"})
        dispatcher = make_dispatcher(make_config(), client)
        await request_module.handle_request(dispatcher, request_event())
        self.assertEqual(len(client.sent), 1)
        group_id, text = client.sent[0]
        self.assertEqual(group_id, 300)
        self.assertIn("202", text)
        self.assertIn("答案是小汐", text)
        self.assertIn("同意", text)
        entry = self.load_pending()["flag-abc"]
        self.assertEqual(entry["announce_message_id"], 12345)
        self.assertGreater(entry["announce_expires_at"], time.time())

    async def test_no_announce_when_bot_not_admin(self):
        client = FakeClient(roles={888: "member"})
        dispatcher = make_dispatcher(make_config(), client)
        await request_module.handle_request(dispatcher, request_event())
        self.assertEqual(client.sent, [])
        entry = self.load_pending()["flag-abc"]
        self.assertNotIn("announce_message_id", entry)

    async def test_no_announce_when_join_review_off(self):
        client = FakeClient(roles={888: "admin"})
        dispatcher = make_dispatcher(make_config(join_review=False), client)
        await request_module.handle_request(dispatcher, request_event())
        self.assertEqual(client.sent, [])

    async def test_announce_truncates_long_comment(self):
        client = FakeClient(roles={888: "owner"})
        dispatcher = make_dispatcher(make_config(), client)
        await request_module.handle_request(
            dispatcher, request_event(comment="长" * 500))
        _group_id, text = client.sent[0]
        self.assertNotIn("长" * 201, text)


class JoinReviewMatchTests(JoinReviewBase):
    def dispatcher(self, roles=None, join_review=True):
        client = FakeClient(roles=roles or {201: "admin"})
        return make_dispatcher(make_config(join_review=join_review), client), client

    async def test_reply_segment_matches_announce_message(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意", reply_id=12345))
        self.assertTrue(handled)
        self.assertEqual(
            client.calls, [("set_group_add_request", "flag-abc", "add", True, "")])
        self.assertIn("已同意", client.sent[-1][1])
        self.assertIn("202", client.sent[-1][1])
        entry = self.load_pending()["flag-abc"]
        self.assertTrue(entry["handled"])
        self.assertEqual(entry["handled_by"], 201)

    async def test_single_candidate_without_reply(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("  拒绝了吧  "))
        self.assertTrue(handled)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(client.calls[0][3])
        self.assertIn("已拒绝", client.sent[-1][1])

    async def test_multiple_candidates_prompt_for_reply(self):
        seed_pending(self.pending_path, {
            "flag-abc": pending_entry(),
            "flag-def": pending_entry(
                flag="flag-def", user_id=203, announce_message_id=12346),
        })
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意"))
        self.assertTrue(handled)
        self.assertEqual(client.calls, [])
        self.assertIn("多条待审批", client.sent[-1][1])

    async def test_reply_to_unrelated_message_not_intercepted(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意", reply_id=99999))
        self.assertFalse(handled)
        self.assertEqual(client.calls, [])

    async def test_plain_member_not_intervened(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher(roles={201: "member"})
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意", role="member"))
        self.assertFalse(handled)
        self.assertEqual(client.calls, [])

    async def test_join_review_off_not_intercepted(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher(join_review=False)
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意"))
        self.assertFalse(handled)
        self.assertEqual(client.calls, [])

    async def test_no_pending_not_intercepted(self):
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意"))
        self.assertFalse(handled)

    async def test_handled_entry_not_processed_twice(self):
        seed_pending(self.pending_path, {
            "flag-abc": pending_entry(handled=True),
        })
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意"))
        self.assertFalse(handled)
        self.assertEqual(client.calls, [])

    async def test_expired_entry_not_processed(self):
        seed_pending(self.pending_path, {
            "flag-abc": pending_entry(announce_expires_at=time.time() - 1),
        })
        dispatcher, client = self.dispatcher()
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意"))
        self.assertFalse(handled)
        self.assertEqual(client.calls, [])

    async def test_non_text_segment_not_intercepted(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher()
        event = review_message_event("同意")
        event["message"].append(
            {"type": "image", "data": {"file": "x.jpg"}})
        handled = await request_module.try_handle_join_review(dispatcher, event)
        self.assertFalse(handled)

    async def test_approve_failure_reports_reason(self):
        seed_pending(self.pending_path, {"flag-abc": pending_entry()})
        dispatcher, client = self.dispatcher()

        async def fail(flag, sub_type, approve=True, reason=""):
            return {"status": "failed", "wording": "该申请已过期"}

        client.set_group_add_request = fail
        handled = await request_module.try_handle_join_review(
            dispatcher, review_message_event("同意"))
        self.assertTrue(handled)
        self.assertIn("失败", client.sent[-1][1])
        self.assertFalse(self.load_pending()["flag-abc"].get("handled", False))

    async def test_owner_approve_on_handled_entry_does_not_reexecute(self):
        seed_pending(self.pending_path, {
            "flag-abc": pending_entry(handled=True),
        })
        dispatcher, client = self.dispatcher()
        ok, msg = await request_module.approve_request(dispatcher, "flag-abc")
        self.assertTrue(ok)
        self.assertIn("已经", msg)
        self.assertEqual(client.calls, [])

    async def test_owner_approve_by_flag_tail_still_works(self):
        seed_pending(self.pending_path, {
            "flag-abc": pending_entry(),
        })
        dispatcher, client = self.dispatcher()
        ok, _msg = await request_module.approve_request(dispatcher, "abc")
        self.assertTrue(ok)
        self.assertEqual(
            client.calls, [("set_group_add_request", "flag-abc", "add", True, "")])


class JoinReviewIntentTests(unittest.TestCase):
    def test_match_review_intent(self):
        match = request_module._match_review_intent
        self.assertTrue(match("同意"))
        self.assertTrue(match(" 同意吧 "))
        self.assertTrue(match("同意了啊"))
        self.assertFalse(match("拒绝"))
        self.assertFalse(match("拒绝吧"))
        self.assertIsNone(match("不同意"))
        self.assertIsNone(match("同意一下"))
        self.assertIsNone(match(""))
        self.assertIsNone(match(None))


class JoinReviewCommandTests(JoinReviewBase):
    def command_dispatcher(self, root):
        config = make_config(join_review=False)
        return type("Dispatcher", (), {
            "config": config,
            "_config_path": str(Path(root) / "config.json"),
            "_reply": AsyncMock(),
        })()

    async def test_join_review_on_persists(self):
        from bot.commands.moderation import cmd_join_review
        with tempfile.TemporaryDirectory() as root:
            dispatcher = self.command_dispatcher(root)
            await cmd_join_review(dispatcher, 300, 102, "on", "admin", "", [])
            self.assertTrue(
                dispatcher.config["groups"]["300"]["join_review"])
            saved = json.loads(
                Path(dispatcher._config_path).read_text(encoding="utf-8"))
            self.assertTrue(saved["groups"]["300"]["join_review"])
            self.assertIn("已开启", dispatcher._reply.await_args[0][2])

    async def test_join_review_off_persists(self):
        from bot.commands.moderation import cmd_join_review
        with tempfile.TemporaryDirectory() as root:
            dispatcher = self.command_dispatcher(root)
            dispatcher.config["groups"]["300"]["join_review"] = True
            await cmd_join_review(dispatcher, 300, 102, "off", "admin", "", [])
            self.assertFalse(
                dispatcher.config["groups"]["300"]["join_review"])
            self.assertIn("已关闭", dispatcher._reply.await_args[0][2])

    async def test_join_review_bad_args(self):
        from bot.commands.moderation import cmd_join_review
        with tempfile.TemporaryDirectory() as root:
            dispatcher = self.command_dispatcher(root)
            await cmd_join_review(dispatcher, 300, 102, "xxx", "admin", "", [])
            self.assertFalse(dispatcher.config["groups"]["300"]["join_review"])
            self.assertIn("这样用", dispatcher._reply.await_args[0][2])


if __name__ == "__main__":
    unittest.main()
