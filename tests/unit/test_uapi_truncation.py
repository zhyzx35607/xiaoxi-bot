import asyncio
import os
import tempfile
import types
import unittest
from unittest.mock import patch

from bot.integrations import uapi


class _Content:
    def __init__(self, payload):
        self._payload = payload

    async def read(self, size=-1):
        return self._payload


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.headers = {}
        self.content = _Content(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


class UapiTruncationRetryTests(unittest.TestCase):
    def _dispatcher(self, session):
        return types.SimpleNamespace(
            config={"uapi_api_key": "k"},
            client=types.SimpleNamespace(session=session),
        )

    def _run(self, responses):
        session = _Session(responses)
        dispatcher = self._dispatcher(session)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(uapi, "_STATE_PATH", os.path.join(tmp, "s.json")), \
                patch.object(uapi, "_TRUNCATION_RETRY_DELAY", 0):
            uapi.reset_state_for_test()
            result = asyncio.run(
                uapi.uapi_get(dispatcher, "/search/aggregate",
                              params={"q": "test"}))
        uapi.reset_state_for_test()
        return result, session.calls

    def test_truncated_response_is_retried_until_success(self):
        truncated = b'{"results": [{"title": "abc'
        valid = b'{"results": []}'
        result, calls = self._run([
            _Response(truncated),
            _Response(valid),
        ])
        self.assertEqual(result, {"results": []})
        self.assertEqual(calls, 2)

    def test_retries_exhausted_falls_back_to_none(self):
        truncated = b'{"results": [{"title": "abc'
        result, calls = self._run([_Response(truncated) for _ in range(5)])
        self.assertIsNone(result)
        self.assertEqual(calls, 1 + uapi._TRUNCATION_RETRIES)

    def test_invalid_utf8_is_retried(self):
        broken = b'{"title": "\xe4\xb8'
        valid = b'{"results": []}'
        result, calls = self._run([
            _Response(broken),
            _Response(valid),
        ])
        self.assertEqual(result, {"results": []})
        self.assertEqual(calls, 2)

    def test_normal_api_error_is_not_retried(self):
        result, calls = self._run([_Response(b"{}", status=500)])
        self.assertIsNone(result)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
