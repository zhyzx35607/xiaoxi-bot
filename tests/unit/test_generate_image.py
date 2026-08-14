import asyncio
import unittest
from unittest.mock import patch

from bot.ai.providers import generate_image


class _FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    instances = []
    response = _FakeResponse(payload={"data": [{"url": "https://img.test/a.png"}]})

    def __init__(self):
        self.requests = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return type(self).response


class GenerateImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeSession.instances = []
        _FakeSession.response = _FakeResponse(
            payload={"data": [{"url": "https://img.test/a.png"}]})
        self.dispatcher = type("Dispatcher", (), {
            "config": {
                "agnes_api_key": "test-key",
                "agnes_base_url": "https://agnes.test/v1",
            },
        })()
        self._env = patch.dict("os.environ", {
            "AGNES_API_KEY": "", "QQBOT_AGNES_API_KEY": "",
            "AGNES_BASE_URL": "", "AGNES_MODEL": "",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()

    async def test_creates_own_session_when_none_given(self):
        with patch("bot.ai.providers.aiohttp.ClientSession", _FakeSession):
            url, err = await generate_image(self.dispatcher, "一只猫")
        self.assertIsNone(err)
        self.assertEqual(url, "https://img.test/a.png")
        self.assertEqual(len(_FakeSession.instances), 1)
        session = _FakeSession.instances[0]
        self.assertEqual(len(session.requests), 1)
        request_url, kwargs = session.requests[0]
        self.assertEqual(request_url, "https://agnes.test/v1/images/generations")
        self.assertEqual(kwargs["json"]["prompt"], "一只猫")
        self.assertEqual(kwargs["timeout"].total, 60)

    async def test_http_error_returns_failure_message(self):
        _FakeSession.response = _FakeResponse(status=500, text="boom")
        with patch("bot.ai.providers.aiohttp.ClientSession", _FakeSession):
            url, err = await generate_image(self.dispatcher, "一只猫")
        self.assertIsNone(url)
        self.assertEqual(err, "生图失败 (HTTP 500)")

    async def test_network_error_returns_error_message(self):
        class FailingSession(_FakeSession):
            def post(self, url, **kwargs):
                raise RuntimeError("conn reset")

        with patch("bot.ai.providers.aiohttp.ClientSession", FailingSession):
            url, err = await generate_image(self.dispatcher, "一只猫")
        self.assertIsNone(url)
        self.assertEqual(err, "生图出错: conn reset")

    async def test_timeout_returns_retry_message(self):
        class SlowSession(_FakeSession):
            def post(self, url, **kwargs):
                raise asyncio.TimeoutError()

        with patch("bot.ai.providers.aiohttp.ClientSession", SlowSession):
            url, err = await generate_image(self.dispatcher, "一只猫")
        self.assertIsNone(url)
        self.assertEqual(err, "生图超时了，再试一次吧")

    async def test_missing_api_key_short_circuits(self):
        self.dispatcher.config = {}
        url, err = await generate_image(self.dispatcher, "一只猫")
        self.assertIsNone(url)
        self.assertEqual(err, "Agnes API key not configured")

    async def test_uses_provided_session_without_creating_new_one(self):
        class ProvidedSession:
            def __init__(self):
                self.requests = []

            def post(self, url, **kwargs):
                self.requests.append((url, kwargs))
                return _FakeResponse(payload={"data": [{"url": "https://img.test/b.png"}]})

        provided = ProvidedSession()
        with patch("bot.ai.providers.aiohttp.ClientSession", _FakeSession):
            url, err = await generate_image(self.dispatcher, "一只猫", session=provided)
        self.assertEqual((url, err), ("https://img.test/b.png", None))
        self.assertEqual(len(provided.requests), 1)
        self.assertEqual(_FakeSession.instances, [])


if __name__ == "__main__":
    unittest.main()
