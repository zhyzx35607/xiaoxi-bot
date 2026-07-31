"""Delayed natural-reply queue service."""

import asyncio
import heapq
import logging
import random
import time

from ..permission import is_group_enabled

log = logging.getLogger("qqbot")

class DelayedReplyServiceMixin:
    def start_delayed_worker(self):
        if self._delayed_worker_task is not None:
            return
        self._delayed_worker_task = asyncio.create_task(self._delayed_queue_worker())
        log.debug("Delayed reply worker started")

    async def stop_delayed_worker(self):
        if self._delayed_worker_task is None:
            return
        self._delayed_queue_event.set()
        self._delayed_worker_task.cancel()
        try:
            await asyncio.wait_for(self._delayed_worker_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        self._delayed_worker_task = None
        log.debug("Delayed reply worker stopped")

    async def _delayed_queue_worker(self):
        """Single lightweight worker: fire delayed replies when they mature."""
        try:
            while True:
                now = time.time()
                while self._delayed_queue and self._delayed_queue[0][0] <= now:
                    entry = heapq.heappop(self._delayed_queue)
                    if entry[5] is None:
                        continue  # stale entry (merged or cancelled)
                    key = (entry[1], entry[2])
                    if self._delayed_queue_index.get(key) is not entry:
                        continue  # stale entry
                    del self._delayed_queue_index[key]
                    self.create_background_task(
                        self._trigger_delayed_reply(entry[1], entry[2], entry[3], entry[4], entry[5], entry[6]),
                        name="delayed-reply",
                    )
                if not self._delayed_queue:
                    self._delayed_queue_event.clear()
                    await self._delayed_queue_event.wait()
                else:
                    wait = max(0.0, self._delayed_queue[0][0] - time.time())
                    # Clear BEFORE waiting: the event stays set after an
                    # enqueue, and waiting on a set event returns instantly,
                    # which previously spun this loop at full speed and leaked
                    # hundreds of thousands of TimerHandles within seconds.
                    self._delayed_queue_event.clear()
                    try:
                        await asyncio.wait_for(self._delayed_queue_event.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            pass

    async def _enqueue_delayed_reply(self, group_id, user_id, message_id, message, raw, sender_card):
        """Queue a non-explicit interjection to be re-evaluated later.

        Merges per-user entries (keeps only the latest message) and caps total size.
        """
        if not group_id or not user_id:
            return
        key = (group_id, user_id)
        now = time.time()
        existing = self._delayed_queue_index.get(key)
        if existing:
            existing[5] = None  # mark old entry stale
            del self._delayed_queue_index[key]
        if len(self._delayed_queue_index) >= self._delayed_queue_cap:
            log.debug("Delayed queue full, dropping candidate from group=%s user=%s", group_id, user_id)
            return
        delay = random.randint(60, 300)
        entry = [now + delay, group_id, user_id, message_id, message, raw, sender_card]
        heapq.heappush(self._delayed_queue, entry)
        self._delayed_queue_index[key] = entry
        self._delayed_queue_event.set()
        log.debug("Delayed reply queued group=%s user=%s delay=%ds", group_id, user_id, delay)

    async def _trigger_delayed_reply(self, group_id, user_id, message_id, message, raw, sender_card):
        """Re-evaluate a delayed candidate with fresh context and let the AI decide."""
        if not is_group_enabled(self, group_id):
            return
        from ..ai import handle_ai_chat, search_web, _schedule_state
        from ..guard import is_blacklisted

        log.debug("Delayed reply firing group=%s user=%s", group_id, user_id)

        if is_blacklisted(group_id, user_id):
            return
        if message_id and message_id in self._seen_msg_ids:
            return

        import re as _re_clean
        clean_raw = _re_clean.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        if self._is_trivial_for_interjection(clean_raw, message):
            return

        state_key, _ = _schedule_state()
        if state_key == "sleep":
            return

        runtime = self.config.get("runtime", {})
        last_interject = self._group_interject_ts.get(group_id, 0)
        cooldown = runtime.get("non_explicit_judge_cooldown", 240)
        if time.time() - last_interject < cooldown:
            return

        max_consecutive = self.config.get("chat_limits", {}).get("max_consecutive_replies", 5)
        if self._group_consecutive_replies.get(group_id, 0) >= max_consecutive:
            return

        if not self._check_global_rate_limit():
            return

        allowed, remaining = self._check_rate_limit(group_id)
        if not allowed:
            return

        chat_ctx = self._build_chat_context(group_id)
        img_ctx = await self._get_image_context(group_id, message)
        web_ctx = await search_web(self, clean_raw) if self._should_search_web(clean_raw) else ""
        result = await handle_ai_chat(
            self, group_id, user_id, clean_raw, sender_card,
            image_context=img_ctx, chat_context=chat_ctx,
            message_id=message_id, web_search_results=web_ctx,
            reply_intent="自然接话",
            consecutive_replies=self._group_consecutive_replies.get(group_id, 0),
        )
        self._record_ai_outcome(group_id, bool(result))
        if result:
            self._record_bot_reply(group_id, user_id)
            self._record_rate_limit(group_id)
            self._record_global_rate_limit()
            self._group_interject_ts[group_id] = time.time()
            self._group_last_reply_to[(group_id, user_id)] = time.time()
