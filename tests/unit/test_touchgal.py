import types
import unittest
from unittest.mock import AsyncMock, patch

from bot.integrations import touchgal


class _Response:
    def __init__(self, payload, status=200, json_error=None):
        self.status = status
        self.payload = payload
        self.json_error = json_error

    async def json(self, content_type=None):
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    """Fake aiohttp session; queued entries may be _Response or Exception."""

    def __init__(self, *steps):
        self._steps = list(steps)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _dispatcher(config=None, session=None):
    return types.SimpleNamespace(
        config=config or {},
        client=types.SimpleNamespace(session=session),
    )


_SEARCH_PAYLOAD = {
    "success": True,
    "data": {
        "items": [
            {
                "uniqueId": "Abcd1234",
                "name": "千恋万花",
                "aliases": ["Senren Banka", "せんれんばんか"],
            },
        ],
    },
}

_RESOURCES_PAYLOAD = {
    "success": True,
    "data": {
        "items": [
            {
                "name": "Windows PC版",
                "description": "电脑端资源",
                "categories": ["PC", "汉化"],
                "sizes": ["3.2GB"],
                "deepLink": "https://www.touchgal.ink/game/Abcd1234",
            },
            {
                "name": "安卓直装",
                "description": "手机直装包",
                "categories": ["Android"],
                "sizes": ["1.1GB"],
                "deepLink": "https://www.touchgal.ink/game/Abcd1234#android",
            },
        ],
    },
}


class TouchGalTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        touchgal.reset_cache_for_test()

    def tearDown(self):
        touchgal.reset_cache_for_test()


class SearchRequestTests(TouchGalTestBase):
    async def test_search_builds_request_with_token_and_params(self):
        session = _Session(_Response(_SEARCH_PAYLOAD))
        dispatcher = _dispatcher(
            {"touchgal_api_token": "tok123"}, session,
        )
        result = await touchgal.search_games(dispatcher, "千恋万花", limit=5)
        self.assertTrue(result["ok"])
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, touchgal.DEFAULT_API_BASE + "/v1/games/search")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok123")
        self.assertEqual(kwargs["params"], {
            "keyword": "千恋万花", "page": 1, "limit": 5, "allowNsfw": "false",
        })
        # No proxy configured: aiohttp must receive None, not an empty string.
        self.assertIsNone(kwargs["proxy"])

    async def test_search_limit_is_clamped_to_api_maximum(self):
        session = _Session(_Response(_SEARCH_PAYLOAD))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        await touchgal.search_games(dispatcher, "千恋万花", limit=99)
        self.assertEqual(session.calls[0][1]["params"]["limit"], 10)

    async def test_search_limit_floor_is_one(self):
        session = _Session(_Response(_SEARCH_PAYLOAD))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        await touchgal.search_games(dispatcher, "千恋万花", limit=0)
        self.assertEqual(session.calls[0][1]["params"]["limit"], 1)

    async def test_search_parses_items_and_aliases(self):
        session = _Session(_Response({
            "data": {"items": [{
                "uniqueId": "Abcd1234",
                "name": "千恋万花",
                "aliases": "Senren Banka,せんれんばんか",
                "originalName": "千恋＊万花",
            }]},
        }))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.search_games(dispatcher, "千恋万花")
        item = result["items"][0]
        self.assertEqual(item["unique_id"], "Abcd1234")
        self.assertEqual(item["name"], "千恋万花")
        self.assertIn("Senren Banka", item["aliases"])
        self.assertIn("せんれんばんか", item["aliases"])
        self.assertIn("千恋＊万花", item["aliases"])

    async def test_search_skips_malformed_items(self):
        session = _Session(_Response({"data": {"items": [
            "not-a-dict",
            {"uniqueId": "short", "name": "ID 不合规"},
            {"uniqueId": "Abcd1234"},  # missing name
            {"name": "没有 ID"},
            {"uniqueId": "Efgh5678", "name": "合规作品"},
        ]}}))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.search_games(dispatcher, "合规作品")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["unique_id"], "Efgh5678")

    async def test_search_accepts_top_level_list_payload(self):
        session = _Session(_Response([
            {"uniqueId": "Abcd1234", "name": "千恋万花"},
        ]))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.search_games(dispatcher, "千恋万花")
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["name"], "千恋万花")

    async def test_search_success_false_payload_maps_to_api_error(self):
        session = _Session(_Response(
            {"success": False, "error": {"code": "bad_keyword"}}, status=200,
        ))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.search_games(dispatcher, "千恋万花")
        self.assertEqual(result, {"ok": False, "error": "api_error"})

    async def test_search_non_json_body_degrades_to_empty_items(self):
        session = _Session(_Response(None, json_error=ValueError("not json")))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.search_games(dispatcher, "千恋万花")
        self.assertEqual(result, {"ok": True, "items": []})

    async def test_search_rejects_too_short_query_without_request(self):
        session = _Session()
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.search_games(dispatcher, "a")
        self.assertEqual(result, {"ok": False, "error": "query_too_short"})
        self.assertEqual(session.calls, [])

    async def test_search_cache_hit_avoids_second_request(self):
        session = _Session(_Response(_SEARCH_PAYLOAD))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        first = await touchgal.search_games(dispatcher, "千恋万花")
        second = await touchgal.search_games(dispatcher, "千恋万花")
        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)


class PlatformDetectionTests(unittest.TestCase):
    def test_parse_command_query_maps_each_platform(self):
        cases = (
            ("千恋万花 安卓直装", "android"),
            ("千恋万花 krkr", "krkr"),
            ("千恋万花 pc版", "windows"),
            ("千恋万花 PE版", "pe"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                parsed = touchgal.parse_command_query(raw)
                self.assertEqual(parsed["platform"], expected)
                self.assertEqual(parsed["title"], "千恋万花")

    def test_detect_platform_without_terms_is_empty(self):
        self.assertEqual(touchgal._detect_platform("千恋万花"), "")

    def test_strip_platform_terms_removes_keywords(self):
        self.assertEqual(
            touchgal._strip_platform_terms("安卓 千恋万花 吉里吉里"),
            "千恋万花",
        )

    def test_platform_labels_from_resource_text(self):
        resource = {
            "name": "千恋万花 krkr版",
            "description": "吉里吉里可运行",
            "categories": ["汉化"],
        }
        self.assertIn("KRKR", touchgal._platform_labels(resource))

    def test_resource_matches_platform_via_label_and_terms(self):
        android_resource = {
            "name": "安卓直装", "description": "", "categories": [],
        }
        windows_resource = {
            "name": "电脑端", "description": "Windows 运行", "categories": [],
        }
        self.assertTrue(
            touchgal._resource_matches_platform(android_resource, "android"),
        )
        self.assertTrue(
            touchgal._resource_matches_platform(windows_resource, "windows"),
        )
        self.assertFalse(
            touchgal._resource_matches_platform(windows_resource, "android"),
        )
        self.assertFalse(
            touchgal._resource_matches_platform(android_resource, ""),
        )


class SettingsAndTokenTests(TouchGalTestBase):
    def test_token_state_reporting(self):
        # "/gal status" 展示依赖 _settings 的 token 字段。
        self.assertEqual(touchgal._settings(_dispatcher({}))["token"], "")
        self.assertEqual(
            touchgal._settings(_dispatcher({"touchgal_api_token": "   "}))["token"],
            "",
        )
        self.assertEqual(
            touchgal._settings(_dispatcher({"touchgal_api_token": "  abc "}))["token"],
            "abc",
        )

    def test_disabled_flag_from_config(self):
        settings = touchgal._settings(_dispatcher(
            {"touchgal": {"enabled": False, "auto_reply": False}},
        ))
        self.assertFalse(settings["enabled"])
        self.assertFalse(settings["auto_reply"])

    async def test_api_get_without_token_short_circuits(self):
        session = _Session()
        dispatcher = _dispatcher({}, session)
        result = await touchgal._api_get(dispatcher, "/v1/games/search")
        self.assertEqual(result, {"ok": False, "error": "not_configured"})
        self.assertEqual(session.calls, [])

    async def test_explicit_query_without_token_shows_setup_hint(self):
        result = await touchgal.search_and_format(
            _dispatcher({}), "千恋万花", explicit=True,
        )
        self.assertTrue(result["handled"])
        self.assertIn("Token", result["text"])

    async def test_implicit_query_without_token_stays_silent(self):
        result = await touchgal.search_and_format(
            _dispatcher({}), "千恋万花", explicit=False,
        )
        self.assertFalse(result["handled"])
        self.assertEqual(result["text"], "")

    async def test_disabled_integration_reports_closed_when_explicit(self):
        dispatcher = _dispatcher({"touchgal": {"enabled": False}})
        explicit = await touchgal.search_and_format(
            dispatcher, "千恋万花", explicit=True,
        )
        self.assertIn("未启用", explicit["text"])
        silent = await touchgal.search_and_format(
            dispatcher, "千恋万花", explicit=False,
        )
        self.assertEqual(silent["text"], "")

    async def test_auto_request_without_token_returns_false(self):
        dispatcher = _dispatcher(
            {"touchgal": {"enabled": True, "auto_reply": True}},
        )
        self.assertFalse(
            await touchgal.handle_auto_request(dispatcher, 1, 2, "求千恋万花资源"),
        )


class ProxyValidationTests(TouchGalTestBase):
    def test_loopback_proxies_are_accepted(self):
        for value in (
            "http://127.0.0.1:7890",
            "http://localhost:8080",
            "http://[::1]:8080",
        ):
            with self.subTest(value=value):
                settings = touchgal._settings(_dispatcher(
                    {"touchgal_proxy_url": value},
                ))
                self.assertEqual(settings["proxy"], value)

    def test_non_loopback_or_malformed_proxies_are_rejected(self):
        for value in (
            "http://evil.example:8080",
            "http://10.0.0.2:8080",
            "http://192.168.1.1:1080",
            "https://127.0.0.1:8080",
            "socks5://127.0.0.1:1080",
            "http://127.0.0.1",
            "http://user:pass@127.0.0.1:8080",
            "http://127.0.0.1:8080/?x=1",
            "http://127.0.0.1:8080/#frag",
            "not a url",
            "",
            None,
        ):
            with self.subTest(value=value):
                settings = touchgal._settings(_dispatcher(
                    {"touchgal_proxy_url": value},
                ))
                self.assertEqual(settings["proxy"], "")

    async def test_loopback_proxy_is_forwarded_to_session(self):
        session = _Session(_Response(_SEARCH_PAYLOAD))
        dispatcher = _dispatcher({
            "touchgal_api_token": "t",
            "touchgal_proxy_url": "http://127.0.0.1:7890",
        }, session)
        await touchgal.search_games(dispatcher, "千恋万花")
        self.assertEqual(session.calls[0][1]["proxy"], "http://127.0.0.1:7890")

    async def test_rejected_proxy_is_not_forwarded(self):
        session = _Session(_Response(_SEARCH_PAYLOAD))
        dispatcher = _dispatcher({
            "touchgal_api_token": "t",
            "touchgal_proxy_url": "http://evil.example:8080",
        }, session)
        await touchgal.search_games(dispatcher, "千恋万花")
        self.assertIsNone(session.calls[0][1]["proxy"])


class ApiErrorDegradationTests(TouchGalTestBase):
    async def test_http_status_error_mapping(self):
        cases = (
            (401, "unauthorized"),
            (403, "forbidden"),
            (404, "not_found"),
            (429, "rate_limited"),
            (500, "server_error"),
            (503, "server_error"),
            (400, "api_error"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                session = _Session(_Response({"error": "x"}, status=status))
                dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
                result = await touchgal.search_games(dispatcher, "千恋万花")
                self.assertEqual(result, {"ok": False, "error": expected})

    async def test_timeout_and_connection_errors_degrade(self):
        for error in (TimeoutError("timed out"), OSError("connection reset")):
            with self.subTest(error=error):
                session = _Session(error)
                dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
                result = await touchgal.search_games(dispatcher, "千恋万花")
                self.assertEqual(result, {"ok": False, "error": "network_error"})

    async def test_user_facing_error_messages(self):
        cases = (
            ("unauthorized", "Token 无效"),
            ("rate_limited", "额度"),
            ("network_error", "连接不上"),
            ("server_error", "服务暂时异常"),
        )
        for error, fragment in cases:
            with self.subTest(error=error):
                with patch.object(
                    touchgal, "search_games",
                    new=AsyncMock(return_value={"ok": False, "error": error}),
                ):
                    result = await touchgal.search_and_format(
                        _dispatcher({"touchgal_api_token": "t"}),
                        "千恋万花", explicit=True,
                    )
                self.assertIn(fragment, result["text"])

    async def test_query_too_short_is_handled_even_when_implicit(self):
        with patch.object(
            touchgal, "search_games",
            new=AsyncMock(return_value={"ok": False, "error": "query_too_short"}),
        ):
            result = await touchgal.search_and_format(
                _dispatcher({"touchgal_api_token": "t"}),
                "千恋万花", explicit=False,
            )
        self.assertTrue(result["handled"])
        self.assertIn("太短", result["text"])


class ResourceParsingTests(TouchGalTestBase):
    def test_extract_items_supported_shapes(self):
        items = [{"uniqueId": "Abcd1234"}]
        self.assertEqual(touchgal._extract_items(items), items)
        for key in ("items", "results", "list", "games", "resources"):
            with self.subTest(key=key):
                self.assertEqual(touchgal._extract_items({key: items}), items)
        self.assertEqual(touchgal._extract_items({"data": items}), items)
        self.assertEqual(touchgal._extract_items({"data": {"items": items}}), items)
        self.assertEqual(touchgal._extract_items({"other": 1}), [])
        self.assertEqual(touchgal._extract_items(None), [])
        self.assertEqual(touchgal._extract_items("text"), [])

    async def test_get_resources_rejects_invalid_id_without_request(self):
        session = _Session()
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        for bad in ("short", "waytoolongid123", "含中文的八个字符id", "", None):
            with self.subTest(bad=bad):
                result = await touchgal.get_resources(dispatcher, bad)
                self.assertEqual(result, {"ok": False, "error": "invalid_id"})
        self.assertEqual(session.calls, [])

    async def test_get_resources_filters_nsfw_when_disabled(self):
        payload = {"data": {"items": [
            {"name": "普通资源", "isNsfw": False},
            {"name": "限制资源", "isNsfw": True},
            {"name": "限制资源二", "nsfw": True},
        ]}}
        session = _Session(_Response(payload))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.get_resources(dispatcher, "Abcd1234")
        self.assertEqual([item["name"] for item in result["items"]], ["普通资源"])

    async def test_get_resources_keeps_nsfw_when_allowed(self):
        payload = {"data": {"items": [{"name": "限制资源", "isNsfw": True}]}}
        session = _Session(_Response(payload))
        dispatcher = _dispatcher({
            "touchgal_api_token": "t",
            "touchgal": {"allow_nsfw": True},
        }, session)
        result = await touchgal.get_resources(dispatcher, "Abcd1234")
        self.assertEqual(result["items"][0]["name"], "限制资源")
        self.assertEqual(
            session.calls[0][1]["params"]["allowNsfw"], "true",
        )

    async def test_get_resources_handles_missing_fields(self):
        payload = {"data": {"items": [
            "not-a-dict",
            {
                # name/description/categories 全部缺失
                "deepLink": "https://evil.example/steal",
            },
            {
                "title": "备用名称",
                "summary": "简介",
                "category": "汉化,硬盘版",
                "size": "1.5GB",
                "detail_url": "https://www.touchgal.ink/game/Abcd1234",
            },
        ]}}
        session = _Session(_Response(payload))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        result = await touchgal.get_resources(dispatcher, "Abcd1234")
        self.assertEqual(len(result["items"]), 2)
        fallback, named = result["items"]
        self.assertEqual(fallback["name"], "资源")
        self.assertEqual(fallback["description"], "")
        self.assertEqual(fallback["categories"], [])
        # 外站链接必须被丢弃，不能出现在回复中。
        self.assertEqual(fallback["deep_link"], "")
        self.assertEqual(named["name"], "备用名称")
        self.assertEqual(named["categories"], ["汉化", "硬盘版"])
        self.assertEqual(named["sizes"], ["1.5GB"])
        self.assertEqual(
            named["deep_link"], "https://www.touchgal.ink/game/Abcd1234",
        )

    async def test_get_resources_cache_hit(self):
        session = _Session(_Response(_RESOURCES_PAYLOAD))
        dispatcher = _dispatcher({"touchgal_api_token": "t"}, session)
        await touchgal.get_resources(dispatcher, "Abcd1234")
        await touchgal.get_resources(dispatcher, "Abcd1234")
        self.assertEqual(len(session.calls), 1)


class SearchAndFormatTests(TouchGalTestBase):
    def _configured(self, session):
        return _dispatcher(
            {"touchgal_api_token": "t", "touchgal": {"enabled": True}}, session,
        )

    async def test_success_flow_formats_resources_and_detail_link(self):
        session = _Session(
            _Response(_SEARCH_PAYLOAD), _Response(_RESOURCES_PAYLOAD),
        )
        result = await touchgal.search_and_format(
            self._configured(session), "千恋万花", explicit=True,
        )
        self.assertTrue(result["handled"])
        self.assertEqual(result["selected"]["unique_id"], "Abcd1234")
        self.assertIn("找到《千恋万花》", result["text"])
        self.assertIn("Windows PC版", result["text"])
        self.assertIn("安卓直装", result["text"])
        self.assertIn("https://www.touchgal.ink/game/Abcd1234", result["text"])
        self.assertIn("不是网盘直链", result["text"])

    async def test_platform_preference_reorders_resources(self):
        session = _Session(
            _Response(_SEARCH_PAYLOAD), _Response(_RESOURCES_PAYLOAD),
        )
        result = await touchgal.search_and_format(
            self._configured(session), "千恋万花", platform="android",
            explicit=True,
        )
        self.assertIn("优先查找平台：Android直装", result["text"])
        text = result["text"]
        self.assertLess(text.index("安卓直装"), text.index("Windows PC版"))

    async def test_no_search_results(self):
        session = _Session(_Response({"data": {"items": []}}))
        dispatcher = self._configured(session)
        explicit = await touchgal.search_and_format(
            dispatcher, "不存在的作品", explicit=True,
        )
        self.assertTrue(explicit["handled"])
        self.assertIn("没找到", explicit["text"])
        silent = await touchgal.search_and_format(
            dispatcher, "不存在的作品", explicit=False,
        )
        self.assertFalse(silent["handled"])
        self.assertEqual(silent["text"], "")

    async def test_empty_resources_falls_back_to_detail_page(self):
        session = _Session(
            _Response(_SEARCH_PAYLOAD), _Response({"data": {"items": []}}),
        )
        result = await touchgal.search_and_format(
            self._configured(session), "千恋万花", explicit=True,
        )
        self.assertIn("官方 API 暂未返回公开资源分类", result["text"])
        self.assertIn("https://www.touchgal.ink/Abcd1234", result["text"])

    async def test_resource_failure_still_returns_detail_page(self):
        session = _Session(
            _Response(_SEARCH_PAYLOAD), _Response({}, status=500),
        )
        result = await touchgal.search_and_format(
            self._configured(session), "千恋万花", explicit=True,
        )
        self.assertIn("资源分类暂时读取失败", result["text"])

    async def test_ambiguous_query_lists_candidates_only_when_explicit(self):
        ambiguous = {"data": {"items": [
            {"uniqueId": "Aaaa1111", "name": "作品甲"},
            {"uniqueId": "Bbbb2222", "name": "作品乙"},
        ]}}
        dispatcher = self._configured(_Session(_Response(ambiguous)))
        explicit = await touchgal.search_and_format(
            dispatcher, "作品", explicit=True,
        )
        self.assertTrue(explicit["handled"])
        self.assertIn("作品甲", explicit["text"])
        self.assertIn("作品乙", explicit["text"])

        silent = await touchgal.search_and_format(
            self._configured(_Session(_Response(ambiguous))), "作品",
            explicit=False,
        )
        self.assertFalse(silent["handled"])
        self.assertEqual(silent["text"], "")


class AutoRequestTests(TouchGalTestBase):
    async def test_auto_request_sends_at_and_text(self):
        session = _Session(
            _Response(_SEARCH_PAYLOAD), _Response(_RESOURCES_PAYLOAD),
        )
        send_group_msg = AsyncMock()
        dispatcher = types.SimpleNamespace(
            config={
                "touchgal_api_token": "t",
                "touchgal": {"enabled": True, "auto_reply": True},
            },
            client=types.SimpleNamespace(
                session=session, send_group_msg=send_group_msg,
            ),
        )
        with patch("event_policy.allow_event", return_value=True):
            handled = await touchgal.handle_auto_request(
                dispatcher, 1001, 2002, "求《千恋万花》安卓资源",
            )
        self.assertTrue(handled)
        send_group_msg.assert_awaited_once()
        group_id, segments = send_group_msg.await_args.args
        self.assertEqual(group_id, 1001)
        self.assertEqual(segments[0], {"type": "at", "data": {"qq": "2002"}})
        self.assertIn("找到《千恋万花》", segments[1]["data"]["text"])

    async def test_auto_request_respects_cooldown(self):
        session = _Session()
        dispatcher = types.SimpleNamespace(
            config={
                "touchgal_api_token": "t",
                "touchgal": {"enabled": True, "auto_reply": True},
            },
            client=types.SimpleNamespace(
                session=session, send_group_msg=AsyncMock(),
            ),
        )
        with patch("event_policy.allow_event", return_value=False):
            handled = await touchgal.handle_auto_request(
                dispatcher, 1001, 2002, "求《千恋万花》资源",
            )
        self.assertFalse(handled)
        self.assertEqual(session.calls, [])

    async def test_auto_request_ignores_non_resource_chat(self):
        dispatcher = types.SimpleNamespace(
            config={
                "touchgal_api_token": "t",
                "touchgal": {"enabled": True, "auto_reply": True},
            },
            client=types.SimpleNamespace(
                session=_Session(), send_group_msg=AsyncMock(),
            ),
        )
        self.assertFalse(
            await touchgal.handle_auto_request(
                dispatcher, 1001, 2002, "有没有人今晚一起打游戏",
            ),
        )


class RankingTests(unittest.TestCase):
    def test_select_candidate_exact_match(self):
        items = [{"unique_id": "Abcd1234", "name": "千恋万花", "aliases": []}]
        selected, ranked = touchgal.select_candidate("千恋万花", items)
        self.assertEqual(selected["unique_id"], "Abcd1234")
        self.assertEqual(ranked[0]["score"], 100)

    def test_select_candidate_requires_margin_over_second(self):
        items = [
            {"unique_id": "Aaaa1111", "name": "千恋万花"},
            {"unique_id": "Bbbb2222", "name": "千恋万花FD"},
        ]
        selected, ranked = touchgal.select_candidate("千恋万花", items)
        # 第一名是精确匹配（100 分），即使第二名接近也必须选中。
        self.assertIsNotNone(selected)
        self.assertEqual(ranked[0]["unique_id"], "Aaaa1111")

    def test_select_candidate_returns_none_for_close_scores(self):
        items = [
            {"unique_id": "Aaaa1111", "name": "近似的作品名甲"},
            {"unique_id": "Bbbb2222", "name": "近似的作品名乙"},
        ]
        selected, ranked = touchgal.select_candidate("近似的作品名", items)
        self.assertIsNone(selected)
        self.assertEqual(len(ranked), 2)

    def test_normalize_title_handles_fullwidth_and_symbols(self):
        self.assertEqual(
            touchgal.normalize_title("Ｓｅｎｒｅｎ・Ｂａｎｋａ！"),
            "senrenbanka",
        )


if __name__ == "__main__":
    unittest.main()
