import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot.commands.random_image import (
    RANDOM_IMAGE_HELP,
    _parse_random_image_args,
    cmd_random_image,
)
from bot.integrations import mukyu
from bot.integrations.mukyu import MukyuError, MukyuImage
from bot.permission import LEVEL_MASTER, LEVEL_MEMBER
from app.config import apply_env_overrides, migrate_config


class _Content:
    def __init__(self, payload):
        self.payload = payload

    async def read(self, limit):
        return self.payload[:limit]


class _Response:
    def __init__(self, data, status=200, headers=None):
        self.status = status
        self.payload = json.dumps(data).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.payload)), **(headers or {})}
        self.content = _Content(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _payload(x_restrict=0, local="/i/123.jpg"):
    return {
        "ok": True,
        "data": {
            "image": {
                "id": 123, "x_restrict": x_restrict, "width": 1920,
                "height": 1080, "ext": "jpg", "ai_type": 0,
                "illust_type": 0,
            },
            "urls": {"local": local},
        },
    }


class MukyuClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mukyu.reset_state_for_test()

    async def test_fetch_builds_filters_and_pins_local_origin(self):
        session = _Session(_Response(_payload()))
        dispatcher = type("Dispatcher", (), {
            "config": {"mukyu_images": {"base_url": "https://i.mukyu.ru"}},
            "client": type("Client", (), {"session": session})(),
        })()

        image = await mukyu.fetch_random_image(
            dispatcher, r18=0, tags=["初音ミク", "ボーカロイド"],
            tag_mode="and", orientation="portrait", min_pixels=1000000,
        )

        self.assertEqual(image.url, "https://i.mukyu.ru/i/123.jpg")
        params = session.calls[0][1]["params"]
        self.assertEqual(params["tags"], "初音ミク,ボーカロイド")
        self.assertEqual(params["tag_mode"], "and")
        self.assertEqual(params["r18"], 0)
        self.assertIn("XiaoxiQQBot", session.calls[0][1]["headers"]["User-Agent"])

    async def test_safe_request_rejects_restricted_response(self):
        session = _Session(_Response(_payload(x_restrict=1)))
        dispatcher = type("Dispatcher", (), {
            "config": {}, "client": type("Client", (), {"session": session})(),
        })()
        with self.assertRaises(MukyuError):
            await mukyu.fetch_random_image(dispatcher, r18=0)

    async def test_response_must_use_same_origin_local_path(self):
        session = _Session(_Response(_payload(local="https://example.com/x.jpg")))
        dispatcher = type("Dispatcher", (), {
            "config": {}, "client": type("Client", (), {"session": session})(),
        })()
        with self.assertRaises(MukyuError):
            await mukyu.fetch_random_image(dispatcher)


class RandomImageCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_help_lists_every_supported_filter(self):
        dispatcher = type("Dispatcher", (), {"_reply": AsyncMock()})()
        await cmd_random_image(dispatcher, 100, 200, "help", "member", "", [])
        text = dispatcher._reply.await_args.args[2]
        for value in (
                "标签关系", "横图", "竖图", "方图", "高清", "超清",
                "收藏=1000", "非AI", "AI", "插画", "漫画", "动图",
                "全年龄", "R18", "混合"):
            self.assertIn(value, text)
        self.assertEqual(text, RANDOM_IMAGE_HELP)

    def test_argument_ranges_are_parsed(self):
        parsed = _parse_random_image_args(
            "标签=初音ミク,ボーカロイド 且 竖图 超清 非AI 插画 R18")
        self.assertEqual(parsed["tags"], ["初音ミク", "ボーカロイド"])
        self.assertEqual(parsed["tag_mode"], "and")
        self.assertEqual(parsed["orientation"], "portrait")
        self.assertEqual(parsed["min_pixels"], 4_000_000)
        self.assertEqual(parsed["ai_type"], 0)
        self.assertEqual(parsed["illust_type"], 0)
        self.assertEqual(parsed["r18"], 1)

    async def test_member_cannot_request_r18(self):
        dispatcher = type("Dispatcher", (), {
            "config": {}, "_reply": AsyncMock(),
            "client": type("Client", (), {})(),
        })()
        with patch("bot.commands.random_image.get_user_level", new=AsyncMock(
                return_value=(LEVEL_MEMBER, "member"))), patch(
                "bot.commands.random_image.fetch_random_image", new=AsyncMock()) as fetch:
            await cmd_random_image(
                dispatcher, 100, 200, "R18 标签=初音ミク", "member", "", [])
        fetch.assert_not_awaited()
        self.assertIn("只对最高主人和本群群主人开放", dispatcher._reply.await_args.args[2])

    async def test_group_master_can_request_r18(self):
        image = MukyuImage(
            url="https://i.mukyu.ru/i/123.jpg", image_id=123, x_restrict=1,
            width=1000, height=1000, extension="jpg", ai_type=0, illust_type=0,
        )
        send = AsyncMock(return_value={"status": "ok"})
        dispatcher = type("Dispatcher", (), {
            "config": {}, "_reply": AsyncMock(),
            "client": type("Client", (), {"send_group_msg": send})(),
        })()
        with patch("bot.commands.random_image.get_user_level", new=AsyncMock(
                return_value=(LEVEL_MASTER, "master"))), patch(
                "bot.commands.random_image.fetch_random_image",
                new=AsyncMock(return_value=image)) as fetch:
            await cmd_random_image(
                dispatcher, 100, 200, "R18 竖图", "member", "", [])
        self.assertEqual(fetch.await_args.kwargs["r18"], 1)
        self.assertEqual(send.await_args.args[1][0]["data"]["file"], image.url)

    async def test_send_timeout_does_not_emit_false_failure_message(self):
        image = MukyuImage(
            url="https://i.mukyu.ru/i/123.jpg", image_id=123, x_restrict=0,
            width=1000, height=1000, extension="jpg", ai_type=0, illust_type=0,
        )
        send = AsyncMock(return_value={"status": "timeout", "error_kind": "timeout"})
        dispatcher = type("Dispatcher", (), {
            "config": {}, "_reply": AsyncMock(),
            "client": type("Client", (), {"send_group_msg": send})(),
        })()
        with patch("bot.commands.random_image.get_user_level", new=AsyncMock(
                return_value=(LEVEL_MEMBER, "member"))), patch(
                "bot.commands.random_image.fetch_random_image",
                new=AsyncMock(return_value=image)):
            await cmd_random_image(
                dispatcher, 100, 200, "标签=初音ミク", "member", "", [])
        dispatcher._reply.assert_not_awaited()


class PublicUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_private_address_is_rejected(self):
        from bot.commands import uapi_extra

        addresses = [(None, None, None, None, ("127.0.0.1", 443))]
        with patch.object(uapi_extra.socket, "getaddrinfo", return_value=addresses):
            self.assertEqual(
                await uapi_extra._resolved_public_url("https://example.com/image.jpg"), "")

    async def test_credentials_and_nonstandard_ports_are_rejected(self):
        from bot.commands import uapi_extra

        self.assertEqual(uapi_extra._safe_public_url("https://u:p@example.com/x"), "")
        self.assertEqual(uapi_extra._safe_public_url("https://example.com:8080/x"), "")


class MukyuConfigTests(unittest.TestCase):
    def test_legacy_acg_provider_is_replaced_with_mukyu(self):
        config = {
            "acg_images": {
                "provider": "uapi",
                "collector_interval_seconds": 1,
            },
        }
        migrated, changed = migrate_config(config)
        self.assertTrue(changed)
        self.assertEqual(migrated["acg_images"]["provider"], "mukyu")
        self.assertEqual(migrated["acg_images"]["collector_interval_seconds"], 5)

    def test_agent_schema_upgrade_preserves_existing_limits(self):
        config = {
            "agent": {
                "schema_version": 4,
                "owner_daily_limit": 12,
                "owner_hourly_limit": 3,
                "companion_min_gap_seconds": 1800,
            },
        }
        migrated, changed = migrate_config(config)
        self.assertTrue(changed)
        self.assertEqual(migrated["agent"]["schema_version"], 5)
        self.assertEqual(migrated["agent"]["owner_daily_limit"], 12)
        self.assertEqual(migrated["agent"]["owner_hourly_limit"], 3)
        self.assertEqual(migrated["agent"]["companion_min_gap_seconds"], 1800)

    def test_api_key_is_loaded_only_from_environment(self):
        with patch.dict(os.environ, {"MUKYU_API_KEY": "runtime-key"}, clear=False):
            config = apply_env_overrides({})
        self.assertEqual(config["mukyu_api_key"], "runtime-key")

    def test_runtime_migration_removes_mukyu_key(self):
        from scripts.migrate_runtime_config import remove_env_managed_secrets

        config = {"mukyu_api_key": "persisted"}
        removed = remove_env_managed_secrets(config, {"MUKYU_API_KEY": "runtime"})
        self.assertEqual(removed, ["mukyu_api_key"])
        self.assertNotIn("mukyu_api_key", config)


if __name__ == "__main__":
    unittest.main()
