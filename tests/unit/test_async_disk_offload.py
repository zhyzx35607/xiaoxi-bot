"""Async-path tests: guard cache and command config persistence stay off the event loop."""

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from bot import guard


class GuardAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.blacklist_path = str(Path(self._tmp.name) / "blacklist.json")
        self.warnings_path = str(Path(self._tmp.name) / "r18_warnings.json")
        self._patches = [
            patch.object(guard, "BLACKLIST_FILE", self.blacklist_path),
            patch.object(guard, "R18_WARNING_FILE", self.warnings_path),
        ]
        for patcher in self._patches:
            patcher.start()
        self._reset_cache()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()
        self._tmp.cleanup()
        self._reset_cache()

    @staticmethod
    def _reset_cache():
        guard._bl_cache = None
        guard._bl_cache_ts = 0
        guard._warn_cache = None
        guard._warn_cache_ts = 0

    async def test_is_blacklisted_returns_false_without_entry(self):
        self.assertFalse(await guard.is_blacklisted(100, 9))

    async def test_is_blacklisted_hits_active_entry(self):
        guard.add_blacklist(100, 9, duration_hours=1, bot_owner=1, bot_qq=2)
        self.assertTrue(await guard.is_blacklisted(100, 9))

    async def test_is_blacklisted_purges_expired_entry(self):
        guard.add_blacklist(100, 9, duration_hours=1, bot_owner=1, bot_qq=2)
        entry = guard.load_blacklist()["100_9"]
        entry["expires"] = time.time() - 1
        guard.save_blacklist({"100_9": entry})
        self.assertFalse(await guard.is_blacklisted(100, 9))
        # 过期条目从缓存和磁盘同时清除
        self.assertEqual(guard.load_blacklist(), {})
        on_disk = await asyncio.to_thread(
            lambda: json.loads(Path(self.blacklist_path).read_text(encoding="utf-8")))
        self.assertEqual(on_disk, {})

    async def test_is_blacklisted_offloads_disk_io_to_worker_thread(self):
        loop_thread = threading.get_ident()
        seen_threads = []
        real_load = guard.load_blacklist

        def recording_load():
            seen_threads.append(threading.get_ident())
            return real_load()

        with patch.object(guard, "load_blacklist", side_effect=recording_load):
            self.assertFalse(await guard.is_blacklisted(100, 9))
        self.assertTrue(seen_threads)
        self.assertNotIn(loop_thread, seen_threads)

    async def test_concurrent_checks_do_not_corrupt_cache(self):
        guard.add_blacklist(100, 9, duration_hours=1, bot_owner=1, bot_qq=2)
        results = await asyncio.gather(
            *(guard.is_blacklisted(100, 9) for _ in range(20)))
        self.assertTrue(all(results))

    def test_concurrent_add_blacklist_keeps_all_entries(self):
        def add_range(start):
            for uid in range(start, start + 10):
                guard.add_blacklist(100, uid, duration_hours=1,
                                    bot_owner=1, bot_qq=2)

        threads = [threading.Thread(target=add_range, args=(n * 10,))
                   for n in range(1, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(guard.load_blacklist()), 40)

    async def test_add_warning_and_count_roundtrip_via_thread(self):
        await asyncio.to_thread(guard.add_warning, 100, 9)
        count = await asyncio.to_thread(guard.get_warning_count, 100, 9)
        self.assertEqual(count, 1)


class CommandConfigOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_badword_add_loads_and_commits_off_the_loop(self):
        from bot.commands import common, moderation

        # 群条目按 /启用 的实际结构预置完整 bad_words
        config = {"bot_owner": 1, "bot_qq": 2, "groups": {"100": {
            "enabled": True, "masters": [], "welcome_msg": {},
            "bad_words": {"enabled": True, "auto_delete": True,
                          "warn_msg": "@{user} 请注意文明发言！", "words": []},
            "features": {},
        }}}
        dispatcher = type("DispatcherStub", (), {
            "config": dict(config), "_reply": AsyncMock()})()
        config_ref = dispatcher.config
        loop_thread = threading.get_ident()
        seen_threads = []
        real_load, real_commit = common._load, common._commit

        def recording(fn):
            def wrapper(*args):
                seen_threads.append(threading.get_ident())
                return fn(*args)
            return wrapper

        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "config.json")
            Path(path).write_text(json.dumps(config), encoding="utf-8")
            with patch.object(common, "CONFIG_PATH", path), \
                    patch.object(moderation, "_load",
                                 side_effect=recording(real_load)), \
                    patch.object(moderation, "_commit",
                                 side_effect=recording(real_commit)):
                await moderation.cmd_badword(
                    dispatcher, 100, 10, "add 垃圾话", "member", "", [])
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(saved["groups"]["100"]["bad_words"]["words"], ["垃圾话"])
        # 内存配置原地刷新，长期持有者（AgentRuntime 等）看到同一对象
        self.assertIs(dispatcher.config, config_ref)
        self.assertEqual(
            dispatcher.config["groups"]["100"]["bad_words"]["words"], ["垃圾话"])
        dispatcher._reply.assert_awaited_once()
        self.assertEqual(
            dispatcher._reply.await_args.args[2], "违禁词加好了：垃圾话")
        # _load 和 _commit 都在工作线程执行，不占事件循环
        self.assertEqual(len(seen_threads), 2)
        self.assertNotIn(loop_thread, seen_threads)


class BadwordEmptyDictRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_badword_add_backfills_missing_keys_when_entry_is_empty_dict(self):
        """群条目已由 /欢迎语 等命令预建且 bad_words 为空 dict 时，/违禁词 add 不再 KeyError。"""
        from bot.commands import common, moderation

        config = {"bot_owner": 1, "bot_qq": 2, "groups": {"100": {
            "enabled": True, "masters": [], "welcome_msg": {},
            "bad_words": {},
            "features": {},
        }}}
        dispatcher = type("DispatcherStub", (), {
            "config": dict(config), "_reply": AsyncMock()})()

        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "config.json")
            Path(path).write_text(json.dumps(config), encoding="utf-8")
            with patch.object(common, "CONFIG_PATH", path):
                await moderation.cmd_badword(
                    dispatcher, 100, 10, "add 垃圾话", "member", "", [])
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
        bw = saved["groups"]["100"]["bad_words"]
        self.assertEqual(bw["words"], ["垃圾话"])
        self.assertTrue(bw["enabled"])
        self.assertTrue(bw["auto_delete"])
        self.assertEqual(bw["warn_msg"], "@{user} 请注意文明发言！")
        dispatcher._reply.assert_awaited_once()
        self.assertEqual(
            dispatcher._reply.await_args.args[2], "违禁词加好了：垃圾话")


if __name__ == "__main__":
    unittest.main()
