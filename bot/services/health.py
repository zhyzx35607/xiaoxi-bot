"""Scheduler, push-loop, RSS guard, and background-task lifecycle."""

import asyncio
import logging
import os
import time

from ..storage.runtime_paths import runtime_diagnostic_path

log = logging.getLogger("qqbot")

class HealthServiceMixin:
    def start_scheduler(self):
        """Start the scheduler only when enabled in config (off by default on low-spec hosts)."""
        runtime = self.config.get("runtime", {})
        if not runtime.get("enable_scheduler", False):
            return
        if self._scheduler_task is None:
            from .scheduler import scheduler_loop
            self._scheduler_task = asyncio.create_task(scheduler_loop(self))

    async def stop_scheduler(self):
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=2)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                log.warning("Timed out waiting for scheduler to stop")
        self._scheduler_task = None

    def start_bili_push(self):
        """Start the UP主 new-video polling loop (idles when nothing watched)."""
        if self._bili_push_task is None:
            from ..bilibili import push_loop
            self._bili_push_task = asyncio.create_task(push_loop(self))

    async def stop_bili_push(self):
        if self._bili_push_task and not self._bili_push_task.done():
            self._bili_push_task.cancel()
            try:
                await asyncio.wait_for(self._bili_push_task, timeout=2)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                log.warning("Timed out waiting for bili push loop to stop")
        self._bili_push_task = None

    def start_rss_guard(self):
        """Periodic RSS watchdog: logs growth, gracefully restarts before OOM."""
        if self._rss_guard_task is None:
            self._rss_guard_task = asyncio.create_task(self._rss_guard_loop())
        self._start_rss_thread_watch()

    def _start_rss_thread_watch(self):
        """Daemon-thread RSS sampler: survives a blocked event loop and dumps
        all thread stacks + asyncio task list the moment memory spikes."""
        import faulthandler
        import threading
        existing = getattr(self, "_rss_watch_thread", None)
        if existing and existing.is_alive():
            return
        try:
            self._rss_watch_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._rss_watch_loop = None
        stop_event = threading.Event()
        self._rss_watch_stop = stop_event
        def _watch():
            last = 0.0
            last_error = 0.0
            while not stop_event.wait(1):
                try:
                    kb = self._read_rss_kb()
                    if not kb:
                        continue
                    mb = kb / 1024.0
                    if mb >= 150 and time.time() - last > 20:
                        last = time.time()
                        log.warning("RSS thread-watch: %.0fMB | GC: %s",
                                    mb, self._gc_type_histogram())
                        dump_path = runtime_diagnostic_path("stack_dump.txt")
                        with open(dump_path, "a", encoding="utf-8") as f:
                            f.write("\n=== RSS %.0fMB at %s ===\n" % (
                                mb, time.strftime("%F %T")))
                            faulthandler.dump_traceback(file=f)
                            loop = self._rss_watch_loop
                            if loop is not None:
                                try:
                                    for task in asyncio.all_tasks(loop):
                                        coro = task.get_coro()
                                        f.write("TASK %s %s\n" % (
                                            getattr(coro, "__qualname__", coro),
                                            task.get_name()))
                                except Exception as e:
                                    f.write("task enum failed: %s\n" % e)
                except Exception as exc:
                    now = time.time()
                    if now - last_error >= 60:
                        last_error = now
                        log.warning("RSS thread-watch failed: %s", exc, exc_info=True)
        t = threading.Thread(target=_watch, daemon=True, name="rss-watch")
        self._rss_watch_thread = t
        t.start()

    async def stop_rss_guard(self):
        stop_event = getattr(self, "_rss_watch_stop", None)
        if stop_event:
            stop_event.set()
        if self._rss_guard_task and not self._rss_guard_task.done():
            self._rss_guard_task.cancel()
            try:
                await asyncio.wait_for(self._rss_guard_task, timeout=2)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                pass
        self._rss_guard_task = None

    @staticmethod
    def _read_rss_kb():
        try:
            with open("/proc/self/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
        except Exception:
            return None
        return None

    @staticmethod
    def _gc_type_histogram(limit=15):
        """Top object types by count — cheap snapshot to identify memory hogs."""
        import gc
        from collections import Counter
        counts = Counter(type(obj).__name__ for obj in gc.get_objects())
        return ", ".join("{}={}".format(name, cnt)
                         for name, cnt in counts.most_common(limit))

    async def _rss_guard_loop(self):
        runtime = self.config.get("runtime", {})
        restart_mb = int(runtime.get("rss_restart_mb", 260) or 0)
        log_mb = int(runtime.get("rss_log_mb", 150) or 150)
        last_diag = 0.0
        try:
            while True:
                await asyncio.sleep(5)
                rss_kb = self._read_rss_kb()
                if rss_kb is None:
                    continue
                mb = rss_kb / 1024.0
                if restart_mb > 0 and mb >= restart_mb:
                    log.warning("RSS %.0fMB >= %dMB limit, graceful restart",
                                mb, restart_mb)
                    try:
                        log.warning("GC histogram at restart: %s",
                                    self._gc_type_histogram())
                        self.save_runtime_state(force=True)
                    except Exception:
                        pass
                    os.kill(os.getpid(), 15)  # SIGTERM -> clean shutdown, systemd restarts
                    return
                if mb >= log_mb and time.time() - last_diag > 30:
                    last_diag = time.time()
                    try:
                        log.warning("RSS guard: %.0fMB | GC histogram: %s",
                                    mb, self._gc_type_histogram())
                    except Exception:
                        log.info("RSS guard: %.0fMB", mb)
        except asyncio.CancelledError:
            pass

    def create_background_task(self, coro, name="background"):
        if len(self._background_tasks) >= self._max_background_tasks:
            log.warning("Dropping %s task: background backlog is full", name)
            if hasattr(coro, "close"):
                coro.close()
            return None
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done(t):
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                log.error("%s task failed: %s", name, exc,
                          exc_info=(type(exc), exc, exc.__traceback__))

        task.add_done_callback(_done)
        return task

    async def stop_background_tasks(self):
        tasks = [t for t in self._background_tasks if not t.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for %d background tasks", len(tasks))
