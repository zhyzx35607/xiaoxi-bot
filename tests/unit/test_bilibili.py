"""Unit tests for bot.integrations.bilibili (all network I/O stubbed)."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from bot.integrations import bilibili


def _make_dispatcher(config=None, client=None):
    class DispatcherStub:
        pass

    dispatcher = DispatcherStub()
    dispatcher.config = config if config is not None else {}
    dispatcher.client = client
    return dispatcher


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class _StubContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _StubResponse:
    """Mimics the aiohttp response bits used by the bilibili module."""

    def __init__(self, status=200, payload=None, headers=None, chunks=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self.content = _StubContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._payload


class _StubSession:
    """Serves queued responses (or exceptions) for session.get calls."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected GET: {}".format(url))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _StubClient:
    def __init__(self, session=None):
        self.session = session
        self.sent = []
        self.history_calls = []
        self.send_results = []
        self.history_results = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((group_id, message))
        if self.send_results:
            return self.send_results.pop(0)
        return {"status": "ok"}

    async def get_group_msg_history(self, group_id, count=30):
        self.history_calls.append((group_id, count))
        if self.history_results:
            return self.history_results.pop(0)
        return {"status": "ok", "data": {"messages": []}}


class BiliPureHelperTests(unittest.TestCase):
    def setUp(self):
        bilibili.reset_state_for_test()

    def tearDown(self):
        bilibili.reset_state_for_test()

    def test_extract_bvid_av_b23(self):
        bvid = "BV1xx4y1z7ab"
        self.assertEqual(bilibili.extract_bvid("看看这个 " + bvid), bvid)
        self.assertEqual(bilibili.extract_bvid("没有链接"), "")
        self.assertEqual(bilibili.extract_bvid(None), "")
        self.assertEqual(bilibili.extract_av("av123456 好视频"), 123456)
        self.assertEqual(bilibili.extract_av("AV654321"), 654321)
        self.assertEqual(bilibili.extract_av("av123"), 0)  # too short
        self.assertEqual(bilibili.extract_av(""), 0)
        self.assertEqual(bilibili.extract_b23("点我 https://b23.tv/abc123"),
                         "https://b23.tv/abc123")
        self.assertEqual(bilibili.extract_b23("b23.tv"), "")

    def test_mixin_key_fixed_output(self):
        # Real-world key pair from B站 wbi documentation examples.
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        key = bilibili.mixin_key(img_key, sub_key)
        self.assertEqual(key, "ea1db124af3c7062474693fa704f4ff8")
        self.assertEqual(len(key), 32)

    def test_wbi_sign_fixed_input_fixed_output(self):
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        params = {
            "mid": 12345, "pn": 1, "ps": 5, "order": "pubdate",
            "platform": "web", "web_location": 1550101,
        }
        with patch.object(bilibili.time, "time", return_value=1700000000):
            signed = bilibili.wbi_sign(params, img_key, sub_key)
        self.assertEqual(signed["wts"], 1700000000)
        self.assertEqual(signed["w_rid"], "f42aa54d711a6e3de4aa6b65e69110a3")
        self.assertEqual(signed["mid"], "12345")
        # Original dict must not be mutated.
        self.assertNotIn("wts", params)
        self.assertEqual(params["mid"], 12345)

    def test_wbi_sign_strips_special_chars(self):
        with patch.object(bilibili.time, "time", return_value=1700000000):
            signed = bilibili.wbi_sign({"foo": "a!'()*b"}, "x" * 32, "y" * 32)
        self.assertEqual(signed["foo"], "ab")

    def test_format_duration(self):
        self.assertEqual(bilibili.format_duration(0), "0:00")
        self.assertEqual(bilibili.format_duration(65), "1:05")
        self.assertEqual(bilibili.format_duration(3600), "60:00")
        self.assertEqual(bilibili.format_duration(None), "0:00")

    def test_format_count(self):
        self.assertEqual(bilibili.format_count(0), "0")
        self.assertEqual(bilibili.format_count(9999), "9999")
        self.assertEqual(bilibili.format_count(10000), "1.0万")
        self.assertEqual(bilibili.format_count(123456), "12.3万")

    def test_format_video_text(self):
        video = {
            "title": "测试标题",
            "owner": {"name": "测试UP"},
            "duration": 65,
            "stat": {"view": 12345, "danmaku": 3, "like": 999},
            "desc": "第一行\n第二行",
        }
        text = bilibili.format_video_text(video, "https://link")
        self.assertIn("【B站视频】测试标题", text)
        self.assertIn("UP主：测试UP · 时长 1:05", text)
        self.assertIn("播放 1.2万 · 弹幕 3 · 点赞 999", text)
        self.assertIn("简介：第一行 第二行", text)
        self.assertTrue(text.endswith("https://link"))
        # "-" or empty desc is omitted entirely
        video["desc"] = "-"
        text = bilibili.format_video_text(video, "https://link")
        self.assertNotIn("简介", text)
        # Missing nested fields degrade to defaults instead of raising
        text = bilibili.format_video_text({}, "https://link")
        self.assertIn("UP主：? · 时长 0:00", text)

    def test_history_messages_shapes(self):
        self.assertEqual(bilibili._history_messages({"status": "failed"}), [])
        self.assertEqual(bilibili._history_messages({"status": "ok", "data": [1]}), [1])
        self.assertEqual(
            bilibili._history_messages({"status": "ok", "data": {"messages": [2]}}), [2])
        self.assertEqual(
            bilibili._history_messages({"status": "ok", "data": {"message_list": [3]}}), [3])
        self.assertEqual(bilibili._history_messages({"status": "ok", "data": {}}), [])
        self.assertEqual(bilibili._history_messages("not-a-dict"), [])

    def test_history_record_text_includes_raw_and_serialized(self):
        record = {"raw_message": "正文", "sender": {"user_id": 1}}
        text = bilibili._history_record_text(record)
        self.assertIn("正文", text)
        self.assertIn("user_id", text)
        self.assertEqual(bilibili._history_record_text("junk"), "")

    def test_official_failure_counter_refreshes_session(self):
        bilibili._state["img_key"] = "k"
        bilibili._state["buvid3"] = "b3"
        bilibili._official_failed()
        bilibili._official_failed()
        self.assertEqual(bilibili._state["fail_count"], 2)
        self.assertEqual(bilibili._state["img_key"], "k")
        bilibili._official_failed()
        # Third consecutive failure drops the anon session for a refresh.
        self.assertEqual(bilibili._state["fail_count"], 0)
        self.assertEqual(bilibili._state["img_key"], "")
        self.assertEqual(bilibili._state["buvid3"], "")
        bilibili._official_failed()
        bilibili._official_ok()
        self.assertEqual(bilibili._state["fail_count"], 0)

    def test_delivery_max_attempts_bounds(self):
        self.assertEqual(bilibili._delivery_max_attempts(_make_dispatcher({})), 8)
        self.assertEqual(
            bilibili._delivery_max_attempts(
                _make_dispatcher({"bilibili": {"delivery_max_attempts": 0}})), 8)
        self.assertEqual(
            bilibili._delivery_max_attempts(
                _make_dispatcher({"bilibili": {"delivery_max_attempts": 100}})), 50)
        self.assertEqual(
            bilibili._delivery_max_attempts(
                _make_dispatcher({"bilibili": {"delivery_max_attempts": -3}})), 1)

    def test_sessdata_and_headers(self):
        dispatcher = _make_dispatcher({"bili_sessdata": "  cookie-value  "})
        self.assertEqual(bilibili._sessdata(dispatcher), "cookie-value")
        headers = bilibili._headers(dispatcher=dispatcher)
        self.assertIn("SESSDATA=cookie-value", headers["Cookie"])
        # Empty config means anonymous headers without any Cookie entry.
        anon = _make_dispatcher({"bili_sessdata": ""})
        self.assertEqual(bilibili._sessdata(anon), "")
        headers = bilibili._headers(dispatcher=anon)
        self.assertNotIn("Cookie", headers)

    def test_watched_mids_filters_disabled_and_invalid(self):
        dispatcher = _make_dispatcher({
            "groups": {
                "10001": {"enabled": True, "bili_push": {"mids": [111, "222", "bad"]}},
                "10002": {"enabled": False, "bili_push": {"mids": [333]}},
                "10003": {"enabled": True},
            },
        })
        self.assertEqual(bilibili._watched_mids(dispatcher), {111: ["10001"], 222: ["10001"]})

    def test_at_all_enabled_default_and_override(self):
        dispatcher = _make_dispatcher({
            "groups": {
                "1": {"bili_push": {"at_all": False}},
                "2": {},
            },
        })
        self.assertFalse(bilibili._at_all_enabled(dispatcher, "1"))
        self.assertTrue(bilibili._at_all_enabled(dispatcher, "2"))
        self.assertTrue(bilibili._at_all_enabled(dispatcher, "missing"))

    def test_push_state_migrates_legacy_list_and_caps_seen(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = os.path.join(root, "bili_push.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump({"9001": {"555": ["BV1legacy000"]}}, handle)
            with patch.object(bilibili, "_PUSH_STATE_PATH", state_path):
                bilibili.reset_state_for_test()
                self.assertEqual(bilibili.pushed_bvids("9001", 555), ["BV1legacy000"])
                self.assertEqual(bilibili.push_watermark("9001", 555), 0)
                bilibili.mark_pushed("9001", 555, ["BV1new0000001"], watermark=100)
                bilibili.mark_pushed("9001", 555, [], watermark=50)  # never regresses
                self.assertEqual(bilibili.push_watermark("9001", 555), 100)
                self.assertEqual(bilibili.pushed_bvids("9001", 555),
                                 ["BV1legacy000", "BV1new0000001"])
                # Seen list is capped at 50 entries (oldest dropped).
                bilibili.mark_pushed("9001", 555,
                                     ["BV1bulk{:05d}".format(i) for i in range(60)])
                self.assertEqual(len(bilibili.pushed_bvids("9001", 555)), 50)
                # State round-trips through the JSON file.
                bilibili.reset_state_for_test()
                self.assertEqual(bilibili.push_watermark("9001", 555), 100)
                self.assertIn("BV1bulk00059", bilibili.pushed_bvids("9001", 555))

    def test_parse_dynamic_item_variants(self):
        self.assertIsNone(bilibili.parse_dynamic_item("junk"))
        self.assertIsNone(bilibili.parse_dynamic_item({"type": "DYNAMIC_TYPE_WORD"}))
        # Video uploads are left to the archives poller.
        self.assertIsNone(bilibili.parse_dynamic_item(
            {"id_str": "1", "type": "DYNAMIC_TYPE_AV"}))
        word = {
            "id_str": "1001",
            "type": "DYNAMIC_TYPE_WORD",
            "modules": {
                "module_author": {"mid": 555, "name": "UP主", "pub_ts": 1700000000},
                "module_dynamic": {"major": {"opus": {
                    "summary": {"text": "  今天更新了  "},
                    "pics": [{"url": "//i0.hdslb.com/a.jpg"},
                             {"url": "https://i0.hdslb.com/b.jpg"},
                             {"url": "https://i0.hdslb.com/c.jpg"},
                             {"url": "https://i0.hdslb.com/d.jpg"}],
                }}},
            },
        }
        dyn = bilibili.parse_dynamic_item(word)
        self.assertEqual(dyn["id"], "1001")
        self.assertEqual(dyn["mid"], 555)
        self.assertEqual(dyn["text"], "今天更新了")
        self.assertEqual(len(dyn["images"]), 3)  # capped
        self.assertEqual(dyn["link"], "https://t.bilibili.com/1001")
        # Content-less items are dropped.
        empty = {"id_str": "9", "type": "DYNAMIC_TYPE_WORD", "modules": {}}
        self.assertIsNone(bilibili.parse_dynamic_item(empty))

    def test_parse_dynamic_item_forward(self):
        orig = {
            "id_str": "2002",
            "type": "DYNAMIC_TYPE_WORD",
            "modules": {
                "module_author": {"mid": 777, "name": "原主", "pub_ts": 1},
                "module_dynamic": {"major": {"opus": {"summary": {"text": "原内容"}}}},
            },
        }
        forward = {
            "id_str": "3003",
            "type": "DYNAMIC_TYPE_FORWARD",
            "orig": orig,
            "modules": {
                "module_author": {"mid": 555, "name": "转发者", "pub_ts": 2},
                "module_dynamic": {},
            },
        }
        dyn = bilibili.parse_dynamic_item(forward)
        self.assertEqual(dyn["text"], "转发了 @原主：原内容")
        self.assertEqual(dyn["id"], "3003")

    def test_parse_av_dynamic(self):
        item = {
            "id_str": "1",
            "type": "DYNAMIC_TYPE_AV",
            "modules": {
                "module_author": {"mid": 555, "name": "UP主", "pub_ts": 1700000000},
                "module_dynamic": {"major": {"archive": {
                    "bvid": "BV1xx4y1z7ab", "title": "新视频",
                    "cover": "//i0.hdslb.com/cover.jpg",
                }}},
            },
        }
        video = bilibili.parse_av_dynamic(item)
        self.assertEqual(video["bvid"], "BV1xx4y1z7ab")
        self.assertEqual(video["author"], "UP主")
        self.assertEqual(video["created"], 1700000000)
        self.assertIsNone(bilibili.parse_av_dynamic({"modules": {}}))
        self.assertIsNone(bilibili.parse_av_dynamic(None))


class BiliSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili.reset_state_for_test()

    def tearDown(self):
        bilibili.reset_state_for_test()

    async def test_ensure_session_caches_cookie_and_wbi_keys(self):
        session = _StubSession([
            _StubResponse(payload={"data": {"b_3": "b3", "b_4": "b4"}}),
            _StubResponse(payload={"code": 0, "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }}}),
        ])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        self.assertTrue(await bilibili._ensure_session(dispatcher))
        self.assertEqual(bilibili._state["buvid3"], "b3")
        self.assertEqual(bilibili._state["img_key"], "7cd084941338484aae1ad9425b84077c")
        self.assertEqual(bilibili._state["sub_key"], "4932caff0ff746eab6f01bf08b70ac45")
        # Cached for ~12h: second call issues no further requests.
        self.assertTrue(await bilibili._ensure_session(dispatcher))
        self.assertEqual(len(session.get_calls), 2)

    async def test_ensure_session_fails_without_wbi_keys(self):
        session = _StubSession([
            _StubResponse(payload={"code": 0, "data": {}}),
        ])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        self.assertFalse(await bilibili._ensure_session(dispatcher))
        self.assertEqual(bilibili._state["img_key"], "")

    async def test_ensure_session_survives_network_error(self):
        session = _StubSession([ConnectionError("boom")])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        self.assertFalse(await bilibili._ensure_session(dispatcher))


class BiliVideoInfoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili.reset_state_for_test()

    def tearDown(self):
        bilibili.reset_state_for_test()

    async def test_get_video_info_official_success(self):
        payload = {"code": 0, "data": {"bvid": "BV1xx4y1z7ab", "title": "t"}}
        session = _StubSession([_StubResponse(payload=payload)])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        bilibili._state["fail_count"] = 2
        info = await bilibili.get_video_info(dispatcher, bvid="BV1xx4y1z7ab")
        self.assertEqual(info["title"], "t")
        self.assertEqual(bilibili._state["fail_count"], 0)

    async def test_get_video_info_error_code_falls_back_to_uapi(self):
        session = _StubSession([_StubResponse(payload={"code": -404, "message": "啥都木有"})])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        with patch.object(bilibili.uapi, "uapi_get",
                          new=AsyncMock(return_value={"bvid": "BV1xx4y1z7ab", "aid": 1})) as uapi_mock:
            info = await bilibili.get_video_info(dispatcher, bvid="BV1xx4y1z7ab")
        self.assertEqual(info["aid"], 1)
        self.assertEqual(bilibili._state["fail_count"], 1)
        uapi_mock.assert_awaited_once()

    async def test_get_video_info_network_error_and_uapi_miss_returns_none(self):
        session = _StubSession([TimeoutError("slow")])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        with patch.object(bilibili.uapi, "uapi_get",
                          new=AsyncMock(return_value=None)):
            info = await bilibili.get_video_info(dispatcher, aid=123456)
        self.assertIsNone(info)
        self.assertEqual(bilibili._state["fail_count"], 1)

    async def test_get_playurl_mp4(self):
        ok = {"code": 0, "data": {"durl": [{"url": "https://upos/x.mp4", "size": 123}]}}
        session = _StubSession([_StubResponse(payload=ok)])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        url, size = await bilibili.get_playurl_mp4(dispatcher, "BV1xx4y1z7ab", 99)
        self.assertEqual((url, size), ("https://upos/x.mp4", 123))
        # Error code and HTTP failure both degrade to ("", 0)
        session = _StubSession([_StubResponse(payload={"code": -403})])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        self.assertEqual(await bilibili.get_playurl_mp4(dispatcher, "BV1xx4y1z7ab", 99), ("", 0))
        session = _StubSession([_StubResponse(status=404, payload=None)])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        self.assertEqual(await bilibili.get_playurl_mp4(dispatcher, "BV1xx4y1z7ab", 99), ("", 0))

    async def test_resolve_b23(self):
        session = _StubSession([_StubResponse(
            status=302, headers={"Location": "https://www.bilibili.com/video/BV1xx4y1z7ab"})])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        target = await bilibili.resolve_b23(dispatcher, "https://b23.tv/abc123")
        self.assertEqual(target, "https://www.bilibili.com/video/BV1xx4y1z7ab")
        session = _StubSession([ConnectionError("down")])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        self.assertEqual(await bilibili.resolve_b23(dispatcher, "https://b23.tv/abc123"), "")

    async def test_extract_video_ref(self):
        dispatcher = _make_dispatcher({}, _StubClient(_StubSession()))
        self.assertEqual(
            await bilibili.extract_video_ref(dispatcher, "看 BV1xx4y1z7ab 这个"),
            ("bvid", "BV1xx4y1z7ab"))
        self.assertEqual(
            await bilibili.extract_video_ref(dispatcher, "av123456"),
            ("aid", 123456))
        with patch.object(bilibili, "resolve_b23",
                          new=AsyncMock(return_value="https://www.bilibili.com/video/BV1zz9y8x7cd")):
            self.assertEqual(
                await bilibili.extract_video_ref(dispatcher, "https://b23.tv/xyz"),
                ("bvid", "BV1zz9y8x7cd"))
        self.assertEqual(
            await bilibili.extract_video_ref(dispatcher, "纯文本"),
            ("", None))


class BiliDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili.reset_state_for_test()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        bilibili.reset_state_for_test()

    def _temp_factory(self):
        tmpdir = self._tmp.name

        def factory(prefix, suffix, world_readable=False):
            return tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=tmpdir)

        return factory

    async def test_download_mp4_streams_to_disk(self):
        session = _StubSession([_StubResponse(
            status=200, headers={"Content-Length": "4"}, chunks=[b"ab", b"cd"])])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        with patch.object(bilibili, "create_runtime_temp_file", self._temp_factory()):
            path = await bilibili.download_mp4(dispatcher, "https://upos/x.mp4",
                                               "BV1xx4y1z7ab", 1024)
        self.assertTrue(path)
        self.assertEqual(await asyncio.to_thread(_read_bytes, path), b"abcd")
        os.remove(path)

    async def test_download_mp4_rejects_oversized_content_length(self):
        session = _StubSession([_StubResponse(
            status=200, headers={"Content-Length": "9999"}, chunks=[])])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        with patch.object(bilibili, "create_runtime_temp_file", self._temp_factory()):
            path = await bilibili.download_mp4(dispatcher, "https://upos/x.mp4",
                                               "BV1xx4y1z7ab", 100)
        self.assertIsNone(path)

    async def test_download_mp4_aborts_mid_stream_and_cleans_up(self):
        session = _StubSession([_StubResponse(
            status=200, headers={}, chunks=[b"x" * 60, b"y" * 60])])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        with patch.object(bilibili, "create_runtime_temp_file", self._temp_factory()):
            path = await bilibili.download_mp4(dispatcher, "https://upos/x.mp4",
                                               "BV1xx4y1z7ab", 100)
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self._tmp.name), [], "partial file must be removed")

    async def test_download_mp4_http_error(self):
        session = _StubSession([_StubResponse(status=403)])
        dispatcher = _make_dispatcher({}, _StubClient(session))
        with patch.object(bilibili, "create_runtime_temp_file", self._temp_factory()):
            path = await bilibili.download_mp4(dispatcher, "https://upos/x.mp4",
                                               "BV1xx4y1z7ab", 100)
        self.assertIsNone(path)


class BiliArchivesTests(unittest.IsolatedAsyncioTestCase):
    """UP主 polling: risk-control cooldown, retry degradation, uapi fallback."""

    def setUp(self):
        bilibili.reset_state_for_test()
        # Pretend the anon session is already primed so no cookie/wbi fetch runs.
        # wbi keys are 32-char hex; mixin_key indexes the 64-char pair.
        bilibili._state["img_key"] = "7cd084941338484aae1ad9425b84077c"
        bilibili._state["sub_key"] = "4932caff0ff746eab6f01bf08b70ac45"
        bilibili._state["wbi_ts"] = time.time()

    def tearDown(self):
        bilibili.reset_state_for_test()

    def _dispatcher(self, session, bili_cfg):
        return _make_dispatcher({"bilibili": bili_cfg}, _StubClient(session))

    async def test_archives_success_normalizes_vlist(self):
        vlist = [{
            "bvid": "BV1xx4y1z7ab", "title": "新投稿", "pic": "//pic",
            "created": 1700000000, "length": "01:05", "author": "UP主",
        }]
        session = _StubSession([_StubResponse(
            payload={"code": 0, "data": {"list": {"vlist": vlist}}})])
        dispatcher = self._dispatcher(session, {"uapi_fallback": False})
        videos = await bilibili.get_archives(dispatcher, 555, count=5)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["bvid"], "BV1xx4y1z7ab")
        self.assertEqual(videos[0]["created"], 1700000000)
        self.assertEqual(videos[0]["mid"], 555)
        self.assertEqual(bilibili._state["risk_until"], 0.0)

    async def test_archives_risk_control_pauses_official_polling(self):
        session = _StubSession([_StubResponse(payload={"code": -412})])
        dispatcher = self._dispatcher(
            session, {"official_retries": 1, "uapi_fallback": False})
        before = time.time()
        videos = await bilibili.get_archives(dispatcher, 555)
        self.assertEqual(videos, [])
        # -412 (or -352) triggers a >=300s cooldown on the official endpoint.
        self.assertGreaterEqual(bilibili._state["risk_until"], before + 1800 - 1)
        self.assertEqual(bilibili._state["fail_count"], 1)
        # While cooling down the official endpoint is not even queried.
        videos = await bilibili.get_archives(dispatcher, 555)
        self.assertEqual(videos, [])
        self.assertEqual(len(session.get_calls), 1)

    async def test_archives_recovers_after_cooldown(self):
        bilibili._state["risk_until"] = time.time() - 1  # cooldown expired
        vlist = [{"bvid": "BV1zz9y8x7cd", "title": "恢复", "created": 1}]
        session = _StubSession([_StubResponse(
            payload={"code": 0, "data": {"list": {"vlist": vlist}}})])
        dispatcher = self._dispatcher(session, {"uapi_fallback": False})
        videos = await bilibili.get_archives(dispatcher, 555)
        self.assertEqual([v["bvid"] for v in videos], ["BV1zz9y8x7cd"])
        self.assertEqual(bilibili._state["risk_until"], 0.0)

    async def test_archives_uapi_fallback_is_rate_limited(self):
        session = _StubSession([ConnectionError("risk controlled")])
        dispatcher = self._dispatcher(session, {"official_retries": 1})
        uapi_payload = {"videos": [{
            "bvid": "BV1xx4y1z7ab", "title": "t", "cover": "c",
            "publish_time": 1700000000, "duration": "01:00",
        }]}
        with patch.object(bilibili.uapi, "uapi_get",
                          new=AsyncMock(return_value=uapi_payload)) as uapi_mock:
            videos = await bilibili.get_archives(dispatcher, 555)
            self.assertEqual(len(videos), 1)
            self.assertEqual(videos[0]["created"], 1700000000)
            # Second poll within 5 minutes must not hit the paid endpoint again.
            videos = await bilibili.get_archives(dispatcher, 555)
            self.assertEqual(videos, [])
            self.assertEqual(uapi_mock.await_count, 1)


class BiliDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """_send_group_confirmed: confirmation, backoff, exhaustion."""

    def setUp(self):
        bilibili.reset_state_for_test()

    def tearDown(self):
        bilibili.reset_state_for_test()

    def _dispatcher(self, client, config=None):
        base = {"bot_qq": 222}
        base.update(config or {})
        return _make_dispatcher(base, client)

    async def test_send_success_clears_retry_state(self):
        client = _StubClient()
        dispatcher = self._dispatcher(client)
        key = "bili video:10001:BV1xx4y1z7ab"
        bilibili._delivery_retry_state(dispatcher)[key] = {
            "attempts": 3, "next_retry_at": 0}
        result = await bilibili._send_group_confirmed(
            dispatcher, 10001, [{"type": "text", "data": {"text": "x"}}],
            "BV1xx4y1z7ab", "bili video")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(key, dispatcher._bili_delivery_retries)
        self.assertEqual(len(client.sent), 1)

    async def test_timeout_confirmed_via_history(self):
        client = _StubClient()
        client.send_results = [{"status": "timeout", "error_kind": "timeout"}]
        marker = "BV1xx4y1z7ab"
        no_hit = {"status": "ok", "data": {"messages": [
            {"sender": {"user_id": 222}, "raw_message": "无关消息"}]}}
        hit = {"status": "ok", "data": {"messages": [
            {"sender": {"user_id": 222}, "raw_message": "已发 " + marker}]}}
        client.history_results = [no_hit, hit]
        dispatcher = self._dispatcher(client)
        result = await bilibili._send_group_confirmed(
            dispatcher, 10001, [{"type": "text", "data": {"text": "x"}}],
            marker, "bili video")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_by"], "history_after_timeout")
        # Pre-send history check + one post-timeout confirmation check.
        self.assertEqual(len(client.history_calls), 2)

    async def test_failure_schedules_backoff_then_defers(self):
        client = _StubClient()
        client.send_results = [{"status": "failed", "message": "rate limited"}]
        dispatcher = self._dispatcher(client)
        with self.assertRaises(RuntimeError):
            await bilibili._send_group_confirmed(
                dispatcher, 10001, [], "BV1xx4y1z7ab", "bili video")
        key = "bili video:10001:BV1xx4y1z7ab"
        retry = dispatcher._bili_delivery_retries[key]
        self.assertEqual(retry["attempts"], 1)
        self.assertGreater(retry["next_retry_at"], time.time())
        # Inside the backoff window the same marker defers without re-sending.
        with self.assertRaises(bilibili.BiliDeliveryDeferred):
            await bilibili._send_group_confirmed(
                dispatcher, 10001, [], "BV1xx4y1z7ab", "bili video")
        self.assertEqual(len(client.sent), 1)

    async def test_attempt_cap_records_last_failure_and_gives_up(self):
        client = _StubClient()
        client.send_results = [{"status": "failed", "message": "dead"}]
        dispatcher = self._dispatcher(client)  # default cap: 8 attempts
        key = "bili video:10001:BV1xx4y1z7ab"
        bilibili._delivery_retry_state(dispatcher)[key] = {
            "attempts": 7, "next_retry_at": 0}
        with self.assertRaises(bilibili.BiliDeliveryExhausted):
            await bilibili._send_group_confirmed(
                dispatcher, 10001, [], "BV1xx4y1z7ab", "bili video")
        state = dispatcher._bili_delivery_retries
        self.assertNotIn(key, state)
        failure = state["last_failure"]
        self.assertEqual(failure["reason"], "attempts_exhausted")
        self.assertEqual(failure["attempts"], 8)
        self.assertEqual(failure["marker"], "BV1xx4y1z7ab")
        self.assertEqual(failure["group_id"], "10001")

    async def test_custom_attempt_cap(self):
        client = _StubClient()
        client.send_results = [{"status": "failed"}, {"status": "failed"}]
        dispatcher = self._dispatcher(
            client, {"bilibili": {"delivery_max_attempts": 2}})
        with self.assertRaises(RuntimeError):
            await bilibili._send_group_confirmed(
                dispatcher, 10001, [], "M1", "bili dynamic")
        dispatcher._bili_delivery_retries["bili dynamic:10001:M1"]["next_retry_at"] = 0
        with self.assertRaises(bilibili.BiliDeliveryExhausted):
            await bilibili._send_group_confirmed(
                dispatcher, 10001, [], "M1", "bili dynamic")
        self.assertEqual(
            dispatcher._bili_delivery_retries["last_failure"]["attempts"], 2)

    async def test_history_check_only_trusts_bot_messages(self):
        client = _StubClient()
        # The marker appears, but only in someone else's message.
        client.history_results = [{"status": "ok", "data": {"messages": [
            {"sender": {"user_id": 999}, "raw_message": "引用 BV1xx4y1z7ab"}]}}]
        dispatcher = self._dispatcher(client)
        self.assertFalse(await bilibili._recent_bot_message_contains(
            dispatcher, 10001, "BV1xx4y1z7ab"))
        # History API failure degrades to "not confirmed".
        class BrokenClient(_StubClient):
            async def get_group_msg_history(self, group_id, count=30):
                raise ConnectionError("ws down")

        dispatcher = self._dispatcher(BrokenClient())
        self.assertFalse(await bilibili._recent_bot_message_contains(
            dispatcher, 10001, "BV1xx4y1z7ab"))


class BiliPollLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili.reset_state_for_test()
        self._tmp = tempfile.TemporaryDirectory()
        self._state_patch = patch.object(
            bilibili, "_PUSH_STATE_PATH",
            os.path.join(self._tmp.name, "bili_push.json"))
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._tmp.cleanup()
        bilibili.reset_state_for_test()

    def _dispatcher(self):
        return _make_dispatcher({
            "bot_qq": 222,
            "groups": {"9001": {"enabled": True, "bili_push": {"mids": [555]}}},
        }, _StubClient())

    async def test_poll_once_announces_new_videos_oldest_first(self):
        dispatcher = self._dispatcher()
        videos = [
            {"bvid": "BV1new0000002", "title": "新", "created": 200},
            {"bvid": "BV1old0000001", "title": "旧", "created": 100},
        ]
        announced = []
        with patch.object(bilibili, "get_archives",
                          new=AsyncMock(return_value=videos)), \
                patch.object(bilibili, "_announce_video",
                             new=AsyncMock(side_effect=lambda d, g, v: announced.append(v["bvid"]) or {"status": "ok"})):
            self.assertEqual(await bilibili.poll_once(dispatcher), 2)
        self.assertEqual(announced, ["BV1old0000001", "BV1new0000002"])
        self.assertEqual(bilibili.push_watermark("9001", 555), 200)
        # Second round with the same list announces nothing.
        with patch.object(bilibili, "get_archives",
                          new=AsyncMock(return_value=videos)), \
                patch.object(bilibili, "_announce_video", new=AsyncMock()):
            self.assertEqual(await bilibili.poll_once(dispatcher), 0)

    async def test_poll_once_skips_undeliverable_after_exhaustion(self):
        dispatcher = self._dispatcher()
        videos = [
            {"bvid": "BV1old0000001", "title": "卡死", "created": 100},
            {"bvid": "BV1new0000002", "title": "正常", "created": 200},
        ]

        async def announce(d, g, video):
            if video["bvid"] == "BV1old0000001":
                raise bilibili.BiliDeliveryExhausted("cap hit")
            return {"status": "ok"}

        with patch.object(bilibili, "get_archives",
                          new=AsyncMock(return_value=videos)), \
                patch.object(bilibili, "_announce_video",
                             new=AsyncMock(side_effect=announce)):
            self.assertEqual(await bilibili.poll_once(dispatcher), 1)
        # The exhausted item is marked seen so later uploads are not blocked.
        seen = bilibili.pushed_bvids("9001", 555)
        self.assertIn("BV1old0000001", seen)
        self.assertIn("BV1new0000002", seen)

    async def test_poll_dynamics_once_virgin_only_announces_fresh_newest(self):
        dispatcher = self._dispatcher()
        dispatcher.config["bili_sessdata"] = "cookie"
        now = int(time.time())
        items = [
            {"id_str": "1001", "type": "DYNAMIC_TYPE_WORD", "modules": {
                "module_author": {"mid": 555, "name": "UP", "pub_ts": now - 10},
                "module_dynamic": {"major": {"opus": {"summary": {"text": "新动态"}}}}}},
            {"id_str": "1000", "type": "DYNAMIC_TYPE_WORD", "modules": {
                "module_author": {"mid": 555, "name": "UP", "pub_ts": now - 7200},
                "module_dynamic": {"major": {"opus": {"summary": {"text": "旧动态"}}}}}},
        ]
        announced = []
        with patch.object(bilibili, "get_dynamics_feed",
                          new=AsyncMock(return_value=items)), \
                patch.object(bilibili, "_announce_dynamic",
                             new=AsyncMock(side_effect=lambda d, g, dyn: announced.append(dyn["id"]) or {"status": "ok"})):
            self.assertEqual(await bilibili.poll_dynamics_once(dispatcher), 1)
        # First sight: only the single newest fresh item, never a history flood.
        self.assertEqual(announced, ["1001"])
        entry = bilibili._dyn_entry("9001", 555)
        self.assertIn("1001", entry["dyn_seen"])
        self.assertGreaterEqual(entry["dyn_watermark"], now - 10)


if __name__ == "__main__":
    unittest.main()
