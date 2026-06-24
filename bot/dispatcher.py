# bot/dispatcher.py - Fast message dispatcher with permission system
import asyncio, json, logging, os, random, re, time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from collections import defaultdict, deque
from .permission import (
    get_user_level, get_bot_role, check_permission,
    is_group_enabled, get_group_config, add_master, remove_master, list_masters,
    save_group_config, LEVEL_MASTER, LEVEL_ADMIN, LEVEL_MEMBER
)
from .guard import is_blacklisted, add_blacklist, get_warning_count, add_warning
from .utils import atomic_write_json

log = logging.getLogger("qqbot")


class Dispatcher:
    def __init__(self, config, client, config_path=None):
        self.config = config
        self.client = client
        self._config_path = config_path or os.path.join(_ROOT, "config.json")
        self.commands = {}
        self._lock = asyncio.Lock()
        self._group_msg_counts = defaultdict(lambda: defaultdict(int))
        self._group_msg_buffer = defaultdict(lambda: deque(maxlen=15))
        self._group_repeat_tracker = {}
        self._group_last_interject = {}
        self._group_last_at_bot = {}
        self._group_last_name_reply = {}
        self._group_last_reply_to = {}  # follow-up tracking
        self._group_interject_ts = {}  # last interjection timestamp per group
        self._group_followup_count = {}  # count consecutive followup replies per group
        self._group_at_others_ts = {}  # last time someone @-others (for skip window)
        self._seen_msg_ids = {}  # message_id -> timestamp
        self._seen_msg_ids_maxlen = 2000
        self._daily_likes = {}
        self._daily_fortunes = {}
        self._state_path = os.path.join(_ROOT, "data", "runtime_state.json")
        self._state_dirty = False
        self._last_state_save = 0
        self._message_stat_updates = 0
        self._scheduler_task = None
        self._group_reply_timestamps = {}  # rate limit: group_id -> deque of timestamps
        # Chat limits tracking
        self._group_consecutive_replies = {}  # group_id -> int
        self._group_member_cache = {}  # group_id -> {nickname: qq_id}
        self._member_cache_ts = {}  # group_id -> timestamp
        self._private_processing = {}  # user_id -> timestamp; key presence = processing in-flight
        self._private_consecutive_replies = {}  # user_id -> int; track consecutive bot replies
        self._private_last_reply_ts = {}  # user_id -> timestamp; cooldown between replies
        self._private_urgent_pings = {}  # user_id -> [timestamps]; fast messages during cooldown
        runtime = config.get("runtime", {})
        self._max_background_tasks = int(runtime.get("max_background_tasks", 16))
        self._background_tasks = set()
        self._web_search_cache = {}
        self._search_sem = asyncio.Semaphore(max(1, int(runtime.get("search_concurrency", 1))))
        self._group_last_ai_judge = {}
        self._group_conversation_state = defaultdict(self._new_conversation_state)
        self._load_runtime_state()

    def _new_conversation_state(self):
        return {
            "active_topic": "",
            "last_human_ts": 0,
            "last_bot_ts": 0,
            "human_since_bot": 0,
            "last_decision": None,
            "recent_images": deque(maxlen=4),
        }

    def _load_runtime_state(self):
        try:
            with open(self._state_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            return
        self._daily_likes = state.get("daily_likes", {}) if isinstance(state.get("daily_likes"), dict) else {}
        self._daily_fortunes = state.get("daily_fortunes", {}) if isinstance(state.get("daily_fortunes"), dict) else {}
        counts = state.get("group_msg_counts", {})
        if isinstance(counts, dict):
            for gid, users in counts.items():
                if not isinstance(users, dict):
                    continue
                for uid, count in users.items():
                    try:
                        self._group_msg_counts[int(gid)][int(uid)] = int(count)
                    except Exception:
                        continue

    def save_runtime_state(self, force=False):
        now = time.time()
        if not force and (not self._state_dirty or now - self._last_state_save < 30):
            return
        today = time.strftime("%Y%m%d")
        group_counts = {}
        for gid, users in self._group_msg_counts.items():
            group_counts[str(gid)] = {str(uid): int(cnt) for uid, cnt in users.items()}
        state = {
            "date": today,
            "daily_likes": {k: v for k, v in self._daily_likes.items() if k.startswith(today + ":")},
            "daily_fortunes": {k: v for k, v in self._daily_fortunes.items() if k.startswith(today + ":")},
            "group_msg_counts": group_counts,
            "saved_at": now,
        }
        atomic_write_json(self._state_path, state, indent=2)
        self._state_dirty = False
        self._last_state_save = now
        # Periodic cleanup of stale state (runs with save cycle, no extra timer needed)
        self._cleanup_stale_state()

    def _cleanup_stale_state(self):
        """Purge data for disabled groups and expired entries to prevent unbounded growth."""
        now = time.time()
        groups_cfg = self.config.get("groups", {})
        enabled_gids = {gid for gid, cfg in groups_cfg.items() if cfg.get("enabled", False)}

        # --- A: Remove data for disabled/non-existent groups ---
        all_tracked_gids = set()
        for src in (self._group_msg_counts, self._group_msg_buffer, self._group_repeat_tracker,
                     self._group_last_interject, self._group_last_at_bot, self._group_last_name_reply,
                     self._group_interject_ts, self._group_followup_count, self._group_at_others_ts,
                     self._group_reply_timestamps, self._group_consecutive_replies,
                     self._group_member_cache, self._member_cache_ts,
                     self._group_last_ai_judge, self._group_conversation_state):
            all_tracked_gids.update(str(k) for k in list(src.keys()))

        stale_gids = all_tracked_gids - enabled_gids
        if stale_gids:
            for gid in stale_gids:
                gid_int = int(gid) if gid.lstrip("-").isdigit() else None
                self._group_msg_counts.pop(gid_int, None)
                self._group_msg_buffer.pop(gid_int, None)
                self._group_repeat_tracker.pop(gid_int, None)
                self._group_last_interject.pop(gid_int, None)
                self._group_last_at_bot.pop(gid_int, None)
                self._group_last_name_reply.pop(gid_int, None)
                self._group_interject_ts.pop(gid_int, None)
                self._group_followup_count.pop(gid_int, None)
                self._group_at_others_ts.pop(gid_int, None)
                self._group_reply_timestamps.pop(gid_int, None)
                self._group_consecutive_replies.pop(gid_int, None)
                self._group_member_cache.pop(gid_int, None)
                self._member_cache_ts.pop(gid_int, None)
                self._group_last_ai_judge.pop(gid_int, None)
                self._group_conversation_state.pop(gid_int, None)
            log.info("Cleaned up %d disabled/non-existent groups from runtime state", len(stale_gids))

        # --- B: Expired entries within active groups ---
        # _group_last_reply_to: remove (group, user) entries > 10 min inactive
        stale_reply_to = [(g, u) for (g, u), ts in self._group_last_reply_to.items()
                          if now - ts > 600]
        for key in stale_reply_to:
            del self._group_last_reply_to[key]

        # _group_repeat_tracker: purge empty per-group dicts for active groups
        for gid in list(self._group_repeat_tracker.keys()):
            tracker = self._group_repeat_tracker.get(gid)
            if isinstance(tracker, dict):
                expired_texts = [t for t, v in tracker.items()
                                 if isinstance(v, tuple) and now - v[0] > 300]
                for t in expired_texts:
                    del tracker[t]

        # _daily_likes / _daily_fortunes: remove non-today keys from memory
        today = time.strftime("%Y%m%d")
        for dct in (self._daily_likes, self._daily_fortunes):
            stale_keys = [k for k in dct if not k.startswith(today + ":")]
            for k in stale_keys:
                del dct[k]

        # _private_processing: evict stale entries (> 60s)
        stale_users = [u for u, ts in self._private_processing.items() if now - ts > 60]
        for u in stale_users:
            del self._private_processing[u]

        # _private_last_reply_ts: evict entries older than 2 hours
        stale_priv = [u for u, ts in self._private_last_reply_ts.items() if now - ts > 7200]
        for u in stale_priv:
            del self._private_last_reply_ts[u]
            self._private_consecutive_replies.pop(u, None)
            self._private_urgent_pings.pop(u, None)

        # _last_like_back: evict entries older than 60s (only needed for 1s cooldown)
        if hasattr(self, "_last_like_back"):
            stale_likes = [u for u, ts in self._last_like_back.items() if now - ts > 60]
            for u in stale_likes:
                del self._last_like_back[u]

        # _non_friend_notified: evict entries older than 24h
        if hasattr(self, "_non_friend_notified"):
            stale_nf = [u for u, ts in self._non_friend_notified.items() if now - ts > 86400]
            for u in stale_nf:
                del self._non_friend_notified[u]

        # _image_desc_cache: evict entries older than 1 hour; cap at 500
        if hasattr(self, "_image_desc_cache"):
            img_stale = [k for k, v in self._image_desc_cache.items()
                        if isinstance(v, dict) and now - v.get("ts", 0) > 3600]
            for k in img_stale:
                del self._image_desc_cache[k]
            if len(self._image_desc_cache) > 500:
                oldest = sorted(
                    [(k, v.get("ts", 0) if isinstance(v, dict) else 0)
                     for k, v in self._image_desc_cache.items()],
                    key=lambda x: x[1],
                )[:200]
                for k, _ in oldest:
                    self._image_desc_cache.pop(k, None)

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

    def create_background_task(self, coro, name="background"):
        if len(self._background_tasks) >= self._max_background_tasks:
            log.warning("Dropping %s task: background backlog is full", name)
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

    def register(self, name, handler, help_text="", admin_only=False, owner_only=False,
                 bot_owner=False, bot_admin_required=False, bot_owner_required=False,
                 bot_owner_only=False):
        self.commands[name] = {
            "handler": handler, "help": help_text,
            "admin_only": admin_only, "owner_only": owner_only, "bot_owner": bot_owner,
            "bot_admin_required": bot_admin_required, "bot_owner_required": bot_owner_required,
            "bot_owner_only": bot_owner_only,
        }

    async def dispatch(self, event):
        try:
            pt = event.get("post_type", "")
            if pt == "message":
                await self._handle_message(event)
            elif pt == "notice":
                from .notice_handler import handle_notice
                await handle_notice(self, event)
            elif pt == "request":
                from .request_handler import handle_request
                await handle_request(self, event)
        except Exception as e:
            log.error("Dispatch error: %s", e, exc_info=True)

    async def _handle_message(self, event):
        msg_type = event.get("message_type", "")
        group_id = event.get("group_id", None)
        user_id = event.get("user_id", 0)
        message = event.get("message", [])
        raw = event.get("raw_message", "") or ""
        sender = event.get("sender", {})
        message_id = event.get("message_id", 0)
        prefix = self.config.get("command_prefix", "/")

        # Deduplicate
        if message_id:
            async with self._lock:
                if message_id in self._seen_msg_ids:
                    return
                now_ts = time.time()
                self._seen_msg_ids[message_id] = now_ts
                if len(self._seen_msg_ids) > self._seen_msg_ids_maxlen:
                    sorted_items = sorted(self._seen_msg_ids.items(), key=lambda x: x[1])
                    for old_id, _ in sorted_items[:1000]:
                        del self._seen_msg_ids[old_id]

        # Sender role from NapCat (provided in real-time with each message)
        sender_role = sender.get("role", "member")
        sender_card = sender.get("card") or sender.get("nickname", str(user_id))

        # Group message handling
        if msg_type == "group" and raw:
            # enable/disable are special - only bot_qq can use
            cmd_lower = raw.strip().lower()
            if cmd_lower == prefix + "enable" or cmd_lower == prefix + "disable" or \
               cmd_lower.startswith(prefix + "enable ") or cmd_lower.startswith(prefix + "disable "):
                from .permission import get_user_level, LEVEL_MASTER
                caller_lvl, _ = await get_user_level(self, group_id, user_id, sender_role)
                bot_qq = self.config.get("bot_qq")
                if user_id == self.config.get("bot_owner") or user_id == bot_qq or caller_lvl >= LEVEL_MASTER:
                    parts = raw[len(prefix):].split(maxsplit=1)
                    await self._run_command(
                        parts[0].lower(), parts[1] if len(parts) > 1 else "",
                        group_id, user_id, sender_role, sender_card, message,
                    )
                else:
                    await self.client.send_group_msg(
                        group_id,
                        "这个只有群主人能开关"
                    )
                return

            if not is_group_enabled(self, group_id):
                return

            gcfg = get_group_config(self, group_id)
            feats = gcfg.get("features", {})

            log.debug("[RECV] group=%s user=%s card=%s role=%s raw=%s",
                      group_id, user_id, sender_card, sender_role, raw[:80])

            # Self-message: skip buffer + only process explicit commands
            is_self_msg = user_id == self.config.get("bot_qq")
            # URL safety check before recording message context.
            if not is_self_msg and raw:
                from .security import check_message_urls
                if await check_message_urls(self, group_id, user_id, raw, message_id, sender_role):
                    return
            if not is_self_msg:
                # Message counting
                gc = self._group_msg_counts[group_id]
                gc[user_id] += 1
                self._group_msg_buffer[group_id].append((user_id, raw, time.time(), sender_card))
                self._record_human_turn(group_id, user_id, raw, message)
                self._message_stat_updates += 1
                self._state_dirty = True
                if self._message_stat_updates >= 30:
                    self._message_stat_updates = 0
                    self.save_runtime_state()

            # Collect stickers from image messages
            sticker_cfg = self.config.get("sticker_mode", {})
            if sticker_cfg.get("enabled", True) and sticker_cfg.get("collect", True):
                for seg in message:
                    if seg.get("type") == "image":
                        file_id = seg.get("data", {}).get("file", "")
                        sub_type = seg.get("data", {}).get("sub_type", "0")
                        summary = seg.get("data", {}).get("summary", "")
                        if file_id:
                            from .ai import collect_sticker_async
                            self.create_background_task(
                                collect_sticker_async(self, group_id, file_id, sub_type, summary),
                                name="sticker-collect",
                            )

            # Bad word check
            from .notice_handler import check_bad_words
            if await check_bad_words(self, group_id, user_id, raw, message_id):
                return

            # Repeat check
            if feats.get("repeat", True):
                if await self._check_repeat(group_id, raw, user_id):
                    return

            # Route to handler (skip for self-messages)
            if not is_self_msg:
                await self._handle_group_message(
                    group_id, user_id, message, raw, sender, sender_role, sender_card, message_id
                )
            else:
                # Self-message: only allow explicit commands
                import re as _re_self
                px = self.config.get("command_prefix", "/"); parts = raw[len(px):].split(maxsplit=1) if raw.startswith(px) else []
                if parts:
                    cmd = parts[0].lower()
                    if cmd in self.commands:
                        await self._run_command(cmd, parts[1] if len(parts) > 1 else "",
                                                group_id, user_id, sender_role, sender_card, message)

        elif msg_type == "private" and raw:
            if user_id == self.config.get("bot_owner"):
                await self._handle_owner_private(user_id, message, raw, sender, message_id)
            else:
                # Non-owner private chat → AI auto-reply (no @ trigger needed)
                await self._handle_private_ai_chat(user_id, message, raw, sender, message_id)

    def _check_name_mention(self, raw_message):
        """Check if bot's name is mentioned in message (without @)"""
        nm_cfg = self.config.get("name_mention", {})
        if not nm_cfg.get("enabled", True):
            return False
        names = nm_cfg.get("names", ["小汐", "汐汐"])
        for name in names:
            if name in raw_message:
                return True
        return False

    def _check_followup(self, group_id, user_id):
        key = (group_id, user_id)
        last_ts = self._group_last_reply_to.get(key, 0)
        if time.time() - last_ts > 120:
            return False
        # Check if this user spoke after bot's last reply to them
        buffer = list(self._group_msg_buffer[group_id])
        if not buffer:
            return True  # No buffer = no one else spoke, assume followup
        # Count how many OTHER people spoke after bot replied
        others_spoke = 0
        for uid, raw, ts, card in reversed(buffer):
            if ts <= last_ts:
                break
            if uid != user_id:
                others_spoke += 1
        # Allow 1-2 other messages in between (someone might chip in briefly)
        return others_spoke <= 2

    async def _handle_group_message(self, group_id, user_id, message, raw, sender, sender_role, sender_card, message_id):
        prefix = self.config.get("command_prefix", "/")
        gcfg = get_group_config(self, group_id)
        feats = gcfg.get("features", {})
        is_at_bot = self._check_at_bot(message)
        is_name_mentioned = self._check_name_mention(raw) if not is_at_bot else False
        is_at_others = (not is_at_bot) and self._extract_mentions(message)

        # === BLACKLIST GUARD: check before all interactive features ===
        if is_blacklisted(group_id, user_id):
            log.info("Blocked blacklisted user %s in group %s", user_id, group_id)
            return

        # Strip CQ codes for command matching (e.g. [CQ:reply,id=xxx]/精华 → /精华)
        import re as _re_cmd
        clean_raw = _re_cmd.sub(r"\[CQ:[^\]]+\]", "", raw).strip()

        if clean_raw.startswith(prefix):
            parts = clean_raw[len(prefix):].split(maxsplit=1)
            cmd = parts[0].lower()
            await self._run_command(cmd, parts[1] if len(parts) > 1 else "",
                                    group_id, user_id, sender_role, sender_card, message)
            return

        # Music search
        if feats.get("music", True):
            # Also check natural music triggers
            from .natural_triggers import is_music_trigger
            is_music, music_kw = is_music_trigger(raw)
            if is_music and music_kw:
                from .commands import handle_music_search
                # Create fake raw text with standard prefix for the handler
                fake_raw = "我要点歌 " + music_kw
                if await handle_music_search(self, group_id, user_id, fake_raw, sender_card):
                    return
            else:
                from .commands import handle_music_search
                if await handle_music_search(self, group_id, user_id, raw, sender_card):
                    return

        # === NATURAL LANGUAGE TRIGGERS ===
        from .natural_triggers import check_natural_triggers
        trig = check_natural_triggers(raw, message)
        if trig:
            cmd_name, trig_args = trig
            if cmd_name == "kick":
                for target in trig_args.get("targets", []):
                    await self._run_command("kick", str(target), group_id, user_id, sender_role, sender_card, message)
            elif cmd_name == "ban":
                targets = trig_args.get("targets", [])
                duration = trig_args.get("args", "")
                for target in targets:
                    await self._run_command("ban", f"{duration} {target}".strip(), group_id, user_id, sender_role, sender_card, message)
            elif cmd_name == "unban":
                for target in trig_args.get("targets", []):
                    await self._run_command("unban", str(target), group_id, user_id, sender_role, sender_card, message)
            elif cmd_name in ("like", "fortune", "rank", "精华"):
                await self._run_command(cmd_name, "", group_id, user_id, sender_role, sender_card, message)
            return

        # === NEW AI CHAT LOGIC: Layer 1 rules + Layer 2 AI decision ===
        if feats.get("ai_chat", True):
            from .ai import handle_ai_chat, search_web
            
            # Determine trigger level
            is_explicit_trigger = is_at_bot or is_name_mentioned
            is_image_msg = any(seg.get("type") == "image" for seg in message)
            
            if is_explicit_trigger:
                # @bot or name mention: always respond, reset limits
                self._reset_consecutive_replies(group_id)
                if is_at_bot:
                    self._group_last_at_bot[group_id] = time.time()
                if is_name_mentioned and not is_at_bot:
                    now = time.time()
                    nm_cfg = self.config.get("name_mention", {})
                    cd = nm_cfg.get("cooldown_seconds", 10)
                    last = self._group_last_name_reply.get(group_id, 0)
                    if now - last < cd:
                        return
                    self._group_last_name_reply[group_id] = now
                
                
                # Refresh member cache for @ parsing
                await self._refresh_member_cache(group_id)
                
                # Rate limit check
                allowed, remaining = self._check_rate_limit(group_id)
                if not allowed:
                    await self.client.send_group_msg(group_id, "不行了不行了 刷屏太多 我潜一会 回头聊")
                    return
                
                chat_ctx = self._build_chat_context(group_id)
                img_ctx = await self._get_image_context(group_id, message)
                # Pre-search web for factual questions
                import re as _re_clean3
                clean_msg = _re_clean3.sub(r"\[CQ:[^\]]+\]", "", raw).strip()[:100]
                web_rs = await search_web(self, clean_msg) if self._should_search_web(clean_msg) else ""
                rate_warning = self._get_rate_limit_warning(remaining)
                result = await handle_ai_chat(self, group_id, user_id, raw, sender_card,
                                     image_context=img_ctx, chat_context=chat_ctx,
                                     message_id=message_id, rate_warning=rate_warning,
                                     web_search_results=web_rs,
                                     reply_intent="直接回应",
                                     consecutive_replies=self._group_consecutive_replies.get(group_id, 0))
                if result:
                    self._record_bot_reply(group_id, user_id)
                    self._record_rate_limit(group_id)
                return
            
            # For non-explicit triggers: use local attention scoring first.
            if self._is_short_or_image_only(message, raw):
                return
            # Skip if message @mentions someone else (clearly not talking to us)
            if is_at_others:
                self._group_at_others_ts[group_id] = time.time()
                return
            # Also skip the next 1-2 messages after someone was @-mentioned
            # (they're continuing a conversation with someone specific, not us)
            last_at_others = self._group_at_others_ts.get(group_id, 0)
            if last_at_others and (time.time() - last_at_others) < 15:
                return
            
            is_followup = self._check_followup(group_id, user_id)
            if is_followup or feats.get("interject", True):
                now_ts = time.time()
                
                # === Tiered cooldown logic ===
                if is_followup:
                    # Followup chain limit: max 2 consecutive followup replies per group
                    fup_count = self._group_followup_count.get(group_id, 0)
                    if fup_count >= 2:
                        return  # Already replied twice in a row, let others talk
                else:
                    # Regular interjection: enforce 90s cooldown
                    last_interject = self._group_interject_ts.get(group_id, 0)
                    if (now_ts - last_interject) < 90:
                        return
                    last_judge = self._group_last_ai_judge.get(group_id, 0)
                    judge_cooldown = self.config.get("runtime", {}).get("non_explicit_judge_cooldown", 180)
                    if (now_ts - last_judge) < judge_cooldown:
                        return
                    # Reset followup count when starting a fresh interjection
                    self._group_followup_count[group_id] = 0
                    self._group_last_ai_judge[group_id] = now_ts
                
                from .ai import search_web, handle_ai_chat
                chat_ctx = self._build_chat_context(group_id)
                decision = self._decide_ai_participation(
                    group_id, user_id, message, raw, sender_card,
                    is_followup=is_followup, is_image_msg=is_image_msg,
                )
                self._record_decision(group_id, decision)
                if decision.get("should_reply"):
                    # === Stage 2: AI judgment (for interjections, skip followups to save cost) ===
                    if not is_followup:
                        ai_choice = await self._ai_judge_participation(
                            group_id, user_id, sender_card, raw, chat_ctx or "",
                            is_followup, is_image_msg,
                        )
                        if ai_choice == "SKIP":
                            decision["should_reply"] = False
                            decision["intent"] = "SKIP"
                            decision["reasons"].append("AI判断不该说话")
                            self._record_decision(group_id, decision)
                            return
                        elif ai_choice == "REACT":
                            # Send emoji reaction instead of full reply
                            if message_id:
                                await self._send_emoji_reaction(group_id, message_id, raw)
                            decision["should_reply"] = False
                            decision["intent"] = "REACT"
                            decision["reasons"].append("AI选择表情表态")
                            self._record_decision(group_id, decision)
                            self._group_interject_ts[group_id] = time.time()
                            return
                        # ai_choice == "JOIN": continue to full reply generation

                    allowed, remaining = self._check_rate_limit(group_id)
                    if not allowed:
                        return
                    search_text = raw[:80]
                    if chat_ctx:
                        ctx_lines = chat_ctx.split("\n")[-3:]
                        ctx_text = " ".join([l.split(": ", 1)[-1] if ": " in l else l for l in ctx_lines])
                        if len(ctx_text) > len(search_text):
                            search_text = ctx_text[:200]
                    web_ctx = await search_web(self, search_text) if decision.get("need_search") else ""
                    img_ctx = await self._get_image_context(group_id, message)
                    import re as _re_clean
                    clean_raw = _re_clean.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
                    result = await handle_ai_chat(self, group_id, user_id, clean_raw, sender_card,
                                          image_context=img_ctx, chat_context=chat_ctx,
                                          message_id=message_id, web_search_results=web_ctx,
                                          reply_intent=decision.get("intent", "自然接话"),
                                          consecutive_replies=self._group_consecutive_replies.get(group_id, 0))
                    if result:
                        self._record_bot_reply(group_id, user_id)
                        self._record_rate_limit(group_id)
                        self._group_last_reply_to[(group_id, user_id)] = time.time()
                        self._group_interject_ts[group_id] = time.time()
                        # Track followup chain
                        if is_followup:
                            self._group_followup_count[group_id] = (
                                self._group_followup_count.get(group_id, 0) + 1
                            )
                        else:
                            self._group_followup_count[group_id] = 1
            return



    async def _handle_owner_private(self, user_id, message, raw, sender, message_id):
        """Handle private messages from bot owner: commands only, no auto AI chat."""
        # Blacklist check
        from .guard import is_blacklisted
        if is_blacklisted(0, user_id):
            return

        prefix = self.config.get("command_prefix", "/")

        # Check for command prefix first
        if raw.startswith(prefix):
            parts = raw[len(prefix):].split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            await self._handle_owner_command(cmd, args, user_id, sender, message, raw)
            return

        # Non-command messages from owner: show brief help, don't trigger AI
        await self._reply(None, user_id,
            "输入 / 开头使用命令，/help 查看全部。想测试 AI 聊天请在群聊里 @我。")

    async def _handle_owner_command(self, cmd, args, user_id, sender, message, raw):
        """Route owner private commands to handlers."""
        sender_name = sender.get("nickname", str(user_id))

        if cmd == "help":
            groups_list = ", ".join(str(g) for g in self.config.get("groups", {}).keys()) or "无"
            help_text = f"""小汐管理面板

群组: {groups_list}

/status - 查看状态
/list - 查看所有群组数据概览
/log N - 查看最近N条日志 (默认30)
/bl list - 查看黑名单
/bl add <群号> <QQ> <小时> - 添加黑名单
/bl remove <群号> <QQ> - 移除黑名单
/group enable <群号> - 启用群
/group disable <群号> - 禁用群
/group list - 列出所有群
/memory <群号> - 查看该群的AI记忆
/memory clear <群号> - 清除该群的AI记忆
/sticker <群号> - 查看该群的表情包数量
/sticker clear <群号> - 清除该群的表情包
/sysmsg - 查看入群申请/邀请
/approve flag尾号 - 同意申请
/reject flag尾号 原因 - 拒绝申请
/health - 查看运行状态
/安全 status/log - 查看安全功能和日志
/info <QQ号> - 查看任意人资料
/点赞信息 - 查看点赞统计
"""
            await self._reply(None, user_id, help_text)

        elif cmd in ("enable", "disable"):
            await self._run_command(cmd, args, None, user_id, "member", sender_name, message)

        elif cmd in self._private_group_command_names():
            target_group, rest_args = self._parse_private_group_args(args)
            if not target_group:
                await self._reply(None, user_id, "私聊跨群命令要带群号，比如 /{} 群号 参数".format(cmd))
                return
            await self._run_command(
                cmd, rest_args, target_group, user_id, "member", sender_name, message,
            )

        elif cmd == "log":
            n = 30
            if args.strip():
                try:
                    n = int(args.strip())
                except Exception:
                    pass
            try:
                import subprocess
                log_path = os.path.join(_ROOT, "bot.log")
                result = subprocess.run(["tail", f"-n{n}", log_path],
                                        capture_output=True, text=True, timeout=5)
                await self._reply(None, user_id, result.stdout[-2000:] or "无日志")
            except Exception as e:
                await self._reply(None, user_id, f"读取日志失败: {e}")

        elif cmd == "bl":
            parts2 = args.split()
            if not parts2 or parts2[0] == "list":
                bl = self._load_guard_file(os.path.join(_ROOT, "data", "blacklist.json"))
                if not bl:
                    await self._reply(None, user_id, "黑名单为空")
                    return
                lines = []
                now = time.time()
                for key, entry in bl.items():
                    remaining = max(0, int(entry.get("expires", 0) - now) // 3600)
                    lines.append(f"  g{entry.get('group_id')} u{entry.get('user_id')} 剩余{remaining}h")
                await self._reply(None, user_id, "黑名单：\n" + "\n".join(lines[:30]))
            elif parts2[0] == "add" and len(parts2) >= 4:
                gid = parts2[1]
                uid = parts2[2]
                hours = 48
                try:
                    hours = int(parts2[3]) if len(parts2) > 3 else 48
                except Exception:
                    pass
                add_blacklist(gid, uid, hours, bot_owner=self.config.get("bot_owner"), bot_qq=self.config.get("bot_qq"))
                await self._reply(None, user_id, f"加进黑名单了：群 {gid}，QQ {uid}，{hours} 小时")
            elif parts2[0] == "remove" and len(parts2) >= 3:
                from .guard import remove_blacklist
                remove_blacklist(parts2[1], parts2[2])
                await self._reply(None, user_id, f"移出黑名单了：群 {parts2[1]}，QQ {parts2[2]}")

        elif cmd == "status" or cmd == "state":
            import subprocess
            try:
                bot_state = subprocess.run(["systemctl", "is-active", "qqbot.service"], capture_output=True, text=True, timeout=3)
                napcat_state = subprocess.run(["systemctl", "is-active", "napcat.service"], capture_output=True, text=True, timeout=3)
                def _cn_state(text):
                    value = (text or "").strip()
                    return {"active": "运行中", "inactive": "未运行", "failed": "异常", "activating": "启动中"}.get(value, value or "未知")
                try:
                    with open("/proc/uptime", encoding="utf-8") as f:
                        seconds = int(float(f.read().split()[0]))
                    uptime_text = f"运行时间：{seconds // 86400}天{seconds % 86400 // 3600}小时{seconds % 3600 // 60}分钟"
                except Exception:
                    uptime_text = "运行时间：未知"
                try:
                    meminfo = {}
                    with open("/proc/meminfo", encoding="utf-8") as f:
                        for line in f:
                            key, value = line.split(":", 1)
                            meminfo[key] = int(value.strip().split()[0])
                    total = meminfo.get("MemTotal", 0) // 1024
                    available = meminfo.get("MemAvailable", 0) // 1024
                    swap_total = meminfo.get("SwapTotal", 0) // 1024
                    swap_free = meminfo.get("SwapFree", 0) // 1024
                    mem_text = f"内存：可用 {available} 兆 / 总计 {total} 兆\n交换分区：可用 {swap_free} 兆 / 总计 {swap_total} 兆"
                except Exception:
                    mem_text = "内存：未知"
                status = f"NapCat：{_cn_state(napcat_state.stdout)}\n"
                status += f"小汐：{_cn_state(bot_state.stdout)}\n"
                status += mem_text + "\n"
                status += uptime_text
                await self._reply(None, user_id, status)
            except Exception as e:
                await self._reply(None, user_id, f"状态读取失败：{e}")

        elif cmd == "group" and args.strip():
            parts2 = args.split()
            if parts2[0] == "list":
                groups = self.config.get("groups", {})
                lines = []
                for gid, gcfg in groups.items():
                    st = "开启" if gcfg.get("enabled", True) else "关闭"
                    lines.append(f"  {gid} [{st}]")
                await self._reply(None, user_id, "群组:\n" + "\n".join(lines))
            elif parts2[0] in ("enable", "disable") and len(parts2) >= 2:
                gid = parts2[1]
                enabled = parts2[0] == "enable"
                with open(self._config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                if "groups" not in cfg:
                    cfg["groups"] = {}
                if gid not in cfg["groups"]:
                    cfg["groups"][gid] = json.loads(json.dumps(self.config.get("group_defaults", {})))
                cfg["groups"][gid]["enabled"] = enabled
                atomic_write_json(self._config_path, cfg, indent=2)
                self.config = cfg
                await self._reply(None, user_id, f"群 {gid} 已经{'开了' if enabled else '关了'}")

        elif cmd == "memory" and args.strip():
            parts2 = args.split()
            if parts2[0] == "clear" and len(parts2) >= 2:
                from .ai import clear_group_memory
                clear_group_memory(self, parts2[1])
                await self._reply(None, user_id, f"群 {parts2[1]} 的记忆清掉了")
            else:
                from .ai import _load_memory
                mem = _load_memory(parts2[0])
                if not mem:
                    await self._reply(None, user_id, f"群 {parts2[0]} 无记忆")
                else:
                    lines = []
                    for m in mem[-10:]:
                        role = "小汐" if m.get("role") == "assistant" else "群友"
                        content = (m.get("content") or "")[:80].replace("\n", " ")
                        lines.append(f"[{role}] {content}")
                    await self._reply(None, user_id, f"群 {parts2[0]} 最近记忆:\n" + "\n".join(lines))

        elif cmd == "sticker" and args.strip():
            parts2 = args.split()
            if parts2[0] == "clear" and len(parts2) >= 2:
                import os as _os
                sticker_path = _os.path.join(_ROOT, "data", "stickers", f"group_{parts2[1]}.json")
                if _os.path.exists(sticker_path):
                    _os.remove(sticker_path)
                    await self._reply(None, user_id, f"群 {parts2[1]} 表情包已清除")
                else:
                    await self._reply(None, user_id, f"群 {parts2[1]} 无表情包记录")
            else:
                import os as _os, json as _json
                sticker_path = _os.path.join(_ROOT, "data", "stickers", f"group_{parts2[0]}.json")
                if _os.path.exists(sticker_path):
                    with open(sticker_path) as _sf:
                        stickers = _json.load(_sf)
                    await self._reply(None, user_id, f"群 {parts2[0]} 共有 {len(stickers)} 个表情包")
                else:
                    await self._reply(None, user_id, f"群 {parts2[0]} 无表情包记录")

        elif cmd == "list":
            from .commands import cmd_list
            await cmd_list(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "sysmsg":
            from .commands import cmd_sysmsg
            await cmd_sysmsg(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "approve":
            from .commands import cmd_approve_request
            await cmd_approve_request(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "reject":
            from .commands import cmd_reject_request
            await cmd_reject_request(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "health":
            from .commands import cmd_health
            await cmd_health(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "安全":
            from .commands import cmd_security
            await cmd_security(self, None, user_id, args, "member", sender_name, message)

        elif cmd == "clearai" and args.strip():
            gid = args.strip()
            import glob as _glob, os as _os2
            from .ai import clear_group_memory
            from .guard import load_blacklist, save_blacklist
            clear_group_memory(self, gid)
            sticker_path = _os2.path.join(_os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
                                        "data", "stickers", f"group_{gid}.json")
            if _os2.path.exists(sticker_path):
                _os2.remove(sticker_path)
            bl = load_blacklist()
            prefix_bl = f"{gid}_"
            removed = [k for k in bl if k.startswith(prefix_bl)]
            for k in removed:
                del bl[k]
            if removed:
                save_blacklist(bl)
            try:
                from .guard import load_warnings, save_warnings
                w = load_warnings()
                removed_w = [k for k in w if k.startswith(prefix_bl)]
                for k in removed_w:
                    del w[k]
                if removed_w:
                    save_warnings(w)
            except Exception:
                pass
            user_mem_dir = _os2.path.join(_os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
                                        "data", "memories")
            pattern = _os2.path.join(user_mem_dir, f"group_{gid}_u*.json")
            removed_user_files = 0
            for f in _glob.glob(pattern):
                _os2.remove(f)
                removed_user_files += 1
            await self._reply(None, user_id, f"群 {gid} 的数据清掉了，包括记忆、表情包、黑名单和用户记忆")

        else:
            # Unknown command → just say so, don't trigger AI
            await self._reply(None, user_id, "未知命令，输入 /help 查看可用命令")

    async def _is_friend(self, user_id):
        """Check if user is a friend of the bot (cached, 5 min TTL).

        On API failure: keeps using old cache (extends TTL by 10 min).
        On first-ever call with empty cache: one retry, then lenient (returns True)
        so real friends aren't blocked by a transient timeout.
        """
        now = time.time()
        if not hasattr(self, "_friend_cache"):
            self._friend_cache = set()
            self._friend_cache_ts = 0
            self._friend_fetching = False  # prevent concurrent fetches
        if now - self._friend_cache_ts < 300:
            return user_id in self._friend_cache
        # Prevent concurrent refresh storms
        if getattr(self, "_friend_fetching", False):
            return user_id in self._friend_cache
        self._friend_fetching = True
        try:
            result = await self.client.call("get_friend_list", {})
            if result.get("status") == "ok":
                friends = set()
                for f in result.get("data", []):
                    friends.add(int(f.get("user_id", 0)))
                self._friend_cache = friends
                self._friend_cache_ts = now
                log.info("Friend cache refreshed: %d friends", len(friends))
                return user_id in friends
            # API returned non-ok status
            log.warning("get_friend_list returned %s", result.get("status", "?"))
        except Exception as e:
            log.warning("get_friend_list failed: %s", e)
        finally:
            self._friend_fetching = False
        # API failed: extend TTL of existing cache so we don't hammer it
        if self._friend_cache:
            self._friend_cache_ts = now + 600  # 10 min grace
            log.debug("Friend API failed, using stale cache (%d entries)", len(self._friend_cache))
            return user_id in self._friend_cache
        # Cache is empty (first-ever call failed): be lenient
        log.warning("Friend list never loaded, allowing user %s through", user_id)
        return True

    async def _handle_private_ai_chat(self, user_id, message, raw, sender, message_id):
        """AI auto-reply for non-owner private chat. Friends only, with human-like pacing.

        Key differences from group chat:
        - Longer reply delays (3-120s vs 0.5-60s)
        - Cooldown between replies (20-60s)
        - Short messages and pure stickers are ignored
        - Consecutive reply tracking triggers natural exits
        - Higher slacker rate for occasional "seen-zone" realism
        """
        import re as _re_priv

        # Blacklist check
        from .guard import is_blacklisted
        if is_blacklisted(0, user_id):
            return

        # Dedup: skip if already processing this user (prevents concurrent AI calls)
        now = time.time()
        if user_id in self._private_processing:
            log.debug("Private dedup: skipping user %s (already processing)", user_id)
            return
        self._private_processing[user_id] = now

        # Friend-only gate
        if not await self._is_friend(user_id):
            if not hasattr(self, '_non_friend_notified'):
                self._non_friend_notified = {}
            if user_id not in self._non_friend_notified:
                try:
                    await self.client.send_private_msg(user_id,
                        "你好！我是小汐，目前只有好友才能跟我聊天哦～先加个好友吧")
                except Exception:
                    pass
                self._non_friend_notified[user_id] = now
            self._private_processing.pop(user_id, None)
            return

        # Strip CQ codes for clean text analysis
        clean_raw = _re_priv.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
        has_image = any(seg.get("type") == "image" for seg in message if isinstance(seg, dict))

        # ---- Filter 1: Empty messages ----
        if not clean_raw and not has_image:
            self._private_processing.pop(user_id, None)
            return

        # ---- Filter 2: Pure stickers with no text → ignore ----
        if has_image:
            images_in_msg = [seg for seg in message
                           if isinstance(seg, dict) and seg.get("type") == "image"]
            all_stickers = images_in_msg and all(
                str(seg.get("data", {}).get("sub_type", "0")) != "0"
                for seg in images_in_msg
            )
            if all_stickers and len(clean_raw) < 3:
                self._private_processing.pop(user_id, None)
                return

        # ---- Filter 3: Very short messages without question marks → ignore ----
        if len(clean_raw) < 3 and "?" not in clean_raw and "？" not in clean_raw:
            self._private_processing.pop(user_id, None)
            return

        # ---- Filter 4: Cooldown between replies ----
        last_reply_ts = self._private_last_reply_ts.get(user_id, 0)
        elapsed = now - last_reply_ts
        # If enough time passed (>10 min), reset consecutive count (fresh conversation)
        if elapsed > 600:
            self._private_consecutive_replies[user_id] = 0
            consecutive = 0
        cooldown_min = 20
        cooldown_max = 60
        if elapsed < cooldown_min:
            # Still in hard cooldown — track urgent pings
            urgent = self._private_urgent_pings.setdefault(user_id, [])
            urgent.append(now)
            # Keep only last 10s of pings
            self._private_urgent_pings[user_id] = [t for t in urgent if now - t < 10]
            if len(self._private_urgent_pings[user_id]) < 3:
                self._private_processing.pop(user_id, None)
                return
            # 3+ fast messages during cooldown → they really want to talk, allow
            log.debug("Private cooldown override: user %s sent 3+ urgent messages", user_id)
        elif elapsed < cooldown_max:
            # Soft cooldown: only respond to substantial messages (>= 8 chars or questions)
            if len(clean_raw) < 8 and "?" not in clean_raw and "？" not in clean_raw:
                self._private_processing.pop(user_id, None)
                return

        # ---- Filter 5: Consecutive reply cap → natural exit ----
        consecutive = self._private_consecutive_replies.get(user_id, 0)
        if consecutive >= 6:
            # Already chatted a lot, only respond to explicit goodbyes or urgent questions
            is_goodbye = any(w in clean_raw for w in ("拜", "再见", "晚安", "睡了", "溜", "忙"))
            is_urgent = "?" in clean_raw or "？" in clean_raw or len(clean_raw) > 20
            if not (is_goodbye or is_urgent):
                self._private_processing.pop(user_id, None)
                return
            if is_goodbye:
                # Let it through for a final "bye" response, then reset
                pass

        # ---- Proceed with AI reply ----
        try:
            sender_name = sender.get("nickname", str(user_id))

            # Build image context (only for non-sticker images)
            from .media import extract_message_context
            img_ctx = await extract_message_context(self, None, message)
            if img_ctx:
                img_ctx = img_ctx[:300]

            # Search web for factual questions
            from .ai import search_web
            search_text = clean_raw[:100]
            web_ctx = await search_web(self, search_text) if self._should_search_web(search_text) else ""

            # Call AI chat with actual consecutive count
            from .ai import handle_ai_chat
            result = await handle_ai_chat(
                self, None, user_id, clean_raw, sender_name,
                image_context=img_ctx or "",
                message_id=message_id,
                web_search_results=web_ctx,
                reply_intent="直接回应",
                consecutive_replies=consecutive,
            )
            if result:
                log.debug("Private AI replied to %s(%s)", sender_name, user_id)
                # Track state
                self._private_last_reply_ts[user_id] = time.time()
                self._private_consecutive_replies[user_id] = consecutive + 1
                # Clear urgent pings
                self._private_urgent_pings.pop(user_id, None)
                # Reset consecutive after a natural gap (>10 min)
                if consecutive >= 1:
                    # Schedule reset if no further replies within 10 min
                    pass  # handled by cleanup in _cleanup_stale_state
        finally:
            self._private_processing.pop(user_id, None)


    def _parse_private_group_args(self, args):
        parts = (args or "").strip().split(maxsplit=1)
        if not parts:
            return 0, ""
        if not parts[0].isdigit():
            return 0, args
        return int(parts[0]), parts[1] if len(parts) > 1 else ""

    def _private_group_command_names(self):
        return {
            "kick", "ban", "unban", "allban", "welcome", "badword",
            "admin", "title", "头衔", "精华列表", "群荣誉",
            "群文件", "文件链接", "公告", "ocr", "转发摘要",
            "已读", "history", "禁言列表", "转发", "setgroupavatar",
        }


    async def _refresh_member_cache(self, group_id):
        """Build nickname->QQ cache from recent message buffer (zero API calls).
        Only active speakers are cached — silent members don't need @-resolution."""
        now = time.time()
        if group_id in self._member_cache_ts and now - self._member_cache_ts.get(group_id, 0) < 600:
            return
        cache = {}
        buffer = self._group_msg_buffer.get(group_id, [])
        for user_id, _raw, _ts, sender_card in buffer:
            if sender_card and user_id:
                cache[sender_card] = user_id
        if cache:
            self._group_member_cache[group_id] = cache
            self._member_cache_ts[group_id] = now
            log.debug("Member cache from buffer for group %s: %d speakers", group_id, len(cache))

    async def _get_image_context(self, group_id, message):
        """Return accurate image context. Cache hit → instant. Cache miss → wait for vision API."""
        import html as _html
        contexts = []
        for seg in message:
            if seg.get("type") != "image":
                continue
            data = seg.get("data", {}) if isinstance(seg.get("data"), dict) else {}
            file_id = data.get("file", "")
            summary = data.get("summary", "")
            sub_type = data.get("sub_type", "0")

            # Priority 1: cached vision API result (accurate, fast)
            cache = getattr(self, "_image_desc_cache", None)
            if cache and file_id in cache:
                cached = cache[file_id]
                desc = cached if isinstance(cached, str) else cached.get("desc", "")
                if desc:
                    contexts.append("图片：" + desc[:120])
                    continue

            # Priority 2: call vision API (blocks, but accurate)
            from .ai import describe_image
            desc = await describe_image(self, group_id, file_id, sub_type, summary)
            if desc and desc not in ("[图片]", "[表情/贴纸]"):
                if not hasattr(self, "_image_desc_cache"):
                    self._image_desc_cache = {}
                self._image_desc_cache[file_id] = {"desc": desc, "ts": time.time()}
                contexts.append("图片：" + desc[:120])
            elif summary:
                # Priority 3: QQ summary as fallback when vision API fails
                contexts.append("图片：" + _html.unescape(summary)[:120])
            else:
                contexts.append("[图片]")
        return "\n".join(contexts) if contexts else ""

    async def _enhance_image_cache(self, group_id, file_id, sub_type, summary):
        """Background pre-fetch: warm image cache so future @bot queries hit cache."""
        cache_key = file_id
        if not hasattr(self, "_image_desc_cache"):
            self._image_desc_cache = {}
        if cache_key in self._image_desc_cache:
            return
        try:
            from .ai import describe_image
            desc = await describe_image(self, group_id, file_id, sub_type, summary)
            if desc and desc not in ("[图片]", "[表情/贴纸]"):
                self._image_desc_cache[cache_key] = {"desc": desc, "ts": time.time()}
                # Cap cache: remove oldest entries when over limit
                if len(self._image_desc_cache) > 500:
                    stale = sorted(self._image_desc_cache.items(),
                                   key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0)
                    for k, _ in stale[:200]:
                        del self._image_desc_cache[k]
        except Exception as e:
            log.error("Image enhance cache failed for %s: %s", file_id[:16], e)

    def _load_guard_file(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    async def _run_command(self, cmd, args, group_id, user_id, role, sender_card, message):
        cmd_info = self.commands.get(cmd)
        if not cmd_info:
            return

        # Permission check
        allowed, error = await check_permission(self, group_id, user_id, role, cmd_info)
        if not allowed:
            if error:
                await self._reply(group_id, user_id, error)
            return

        try:
            await cmd_info["handler"](self, group_id, user_id, args, role, sender_card, message)
        except Exception as e:
            log.error("Command %s error: %s", cmd, e, exc_info=True)
            await self._reply(group_id, user_id, "出错了，等会再试。")



    def _check_rate_limit(self, group_id):
        """Check if group has exceeded 100 replies per 30min. Returns (allowed, remaining)."""
        from collections import deque
        cfg = self.config.get("chat_limits", {})
        if not cfg.get("rate_limit_enabled", True):
            return True, 999
        max_replies = cfg.get("max_replies_per_30min", 50)
        now = time.time()
        window = 1800  # 30 minutes
        
        if group_id not in self._group_reply_timestamps:
            self._group_reply_timestamps[group_id] = deque()
        
        stamps = self._group_reply_timestamps[group_id]
        # Clean expired
        while stamps and now - stamps[0] > window:
            stamps.popleft()
        
        remaining = max_replies - len(stamps)
        if remaining <= 0:
            return False, 0
        return True, remaining
    
    def _record_rate_limit(self, group_id):
        """Record a reply timestamp for rate limiting."""
        from collections import deque
        if group_id not in self._group_reply_timestamps:
            self._group_reply_timestamps[group_id] = deque()
        self._group_reply_timestamps[group_id].append(time.time())

    def _record_human_turn(self, group_id, user_id, raw, message):
        state = self._group_conversation_state[group_id]
        now = time.time()
        state["last_human_ts"] = now
        state["human_since_bot"] = state["human_since_bot"] + 1
        if state["human_since_bot"] >= 2:
            self._group_consecutive_replies[group_id] = 0

        text = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        if len(text) >= 4:
            state["active_topic"] = text[:80]
        for seg in message:
            if seg.get("type") == "image":
                summary = seg.get("data", {}).get("summary", "")
                state["recent_images"].append({"ts": now, "summary": summary[:80]})

    def _record_decision(self, group_id, decision):
        self._group_conversation_state[group_id]["last_decision"] = {
            "ts": time.time(),
            "score": decision.get("score", 0),
            "intent": decision.get("intent", ""),
            "reason": ",".join(decision.get("reasons", [])[:5]),
            "chosen": bool(decision.get("should_reply")),
        }
    
    def _get_rate_limit_warning(self, remaining):
        """Get a warning message when approaching limit."""
        if remaining <= 3:
            return "\n（快超限了 我潜了 回头聊）"
        elif remaining <= 10:
            return "\n（今天说不少了 省着点回）"
        return ""

    def _record_bot_reply(self, group_id, user_id):
        """Record that bot replied - only tracks consecutive count."""
        self._group_consecutive_replies[group_id] = (
            self._group_consecutive_replies.get(group_id, 0) + 1
        )
        state = self._group_conversation_state[group_id]
        state["last_bot_ts"] = time.time()
        state["human_since_bot"] = 0
    
    def _reset_consecutive_replies(self, group_id):
        """Reset consecutive reply counter (e.g. when @mentioned)."""
        self._group_consecutive_replies[group_id] = 0
    
    def _is_short_or_image_only(self, message, raw):
        """Check if message is too short or image-only (not worth AI).

        Normal images (sub_type=0) still pass through for vision-based replies.
        Stickers/emoji (sub_type≠0) are treated as emotional expression —
        the sender doesn't expect a description, so we skip unless there's
        meaningful accompanying text.
        """
        import re as _re
        text_only = _re.sub(r'\[CQ:[^\]]+\]', '', raw).strip()

        if message:
            images = [seg for seg in message
                      if isinstance(seg, dict) and seg.get("type") == "image"]
            if images:
                # Check if ALL images are stickers/emoji (sub_type != "0")
                all_stickers = all(
                    str(seg.get("data", {}).get("sub_type", "0")) != "0"
                    for seg in images
                )
                if all_stickers:
                    # Pure sticker with no or trivial text: skip
                    if len(text_only) < 3:
                        return True
                else:
                    # Contains at least one normal image → let it through
                    return False

        # Count non-CQ text for non-image messages
        if len(text_only) < 3:
            return True
        # Check if it is all image/face CQ codes with no text
        if text_only == "" or text_only in [".", "。", "?", "？", "!", "！"]:
            return True
        return False

    def _should_search_web(self, text):
        text = (text or "").strip()
        if len(text) < 4:
            return False
        if re.fullmatch(r"https?://\S+", text):
            return False
        keywords = (
            "什么", "怎么", "为什么", "如何", "多少", "哪个", "哪里", "谁",
            "今天", "现在", "最新", "新闻", "天气", "价格", "时间", "日期",
            "查", "搜索", "资料", "意思", "是否", "有没有", "能不能",
            "是什么", "是谁", "真的假的", "靠谱吗", "出处", "官网", "公告",
            "最近", "刚刚", "新版", "更新", "发布", "什么时候", "哪年",
            "活动", "赛程", "比赛", "排名", "榜单", "分数", "票价", "汇率",
            "涨", "跌", "停服", "开服", "维护", "版本", "参数", "配置",
        )
        if "?" in text or "？" in text or any(k in text for k in keywords):
            return True
        # Mixed ASCII/CJK strings are often titles, software, models, songs, games, or errors.
        return bool(re.search(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", text) and re.search(r"[\u4e00-\u9fff]", text))

    def _decide_ai_participation(self, group_id, user_id, message, raw, sender_card,
                                 is_followup=False, is_image_msg=False):
        """Local low-cost gate for natural group participation."""
        now = time.time()
        cfg = self.config.get("natural_chat", {})
        text = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        score = 0
        reasons = []

        def add(value, reason):
            nonlocal score
            score += value
            reasons.append(reason)

        # Check if this is a pure sticker/emoji message (not a normal image)
        is_pure_sticker = False
        if is_image_msg and message:
            images_in_msg = [seg for seg in message
                           if isinstance(seg, dict) and seg.get("type") == "image"]
            is_pure_sticker = images_in_msg and all(
                str(seg.get("data", {}).get("sub_type", "0")) != "0"
                for seg in images_in_msg
            )

        if is_followup:
            add(58, "对方像是在接着和我聊")
        if self._looks_like_question(text):
            add(26, "像是在问问题")
        if self._should_search_web(text):
            add(18, "像是需要核对事实")
        if self._looks_like_opinion_request(text):
            add(22, "像是在问看法")
        if is_image_msg and not is_pure_sticker and len(text) >= 2:
            add(24, "图片带了说明")
        elif is_pure_sticker and len(text) < 5:
            add(-25, "纯表情包，不值得评价")
        if self._matches_interest_topic(text):
            add(18, "话题适合小汐参与")
        if len(text) >= 8:
            add(8, "内容足够完整")

        recent = [
            item for item in self._group_msg_buffer.get(group_id, [])
            if now - item[2] <= 90
        ]
        active_users = {uid for uid, _, _, _ in recent}
        if len(recent) >= 6 and len(active_users) >= 3:
            add(12, "群聊正在活跃")
        if any(self._check_name_mention(item[1]) for item in recent[-5:]):
            add(15, "最近有人提到小汐")

        if len(text) < 5 and not is_followup:
            add(-32, "消息太短")
        if self._is_low_signal_text(text):
            add(-35, "更像语气词或表情")
        from .ai import is_ai_busy
        if is_ai_busy() and not is_followup:
            add(-28, "AI正在忙")
        if len(self._background_tasks) >= max(2, self._max_background_tasks // 2) and not is_followup:
            add(-18, "后台任务较多")
        last_bot_ts = self._group_conversation_state[group_id].get("last_bot_ts", 0)
        if not is_followup and now - last_bot_ts < cfg.get("quiet_after_reply_seconds", 75):
            add(-30, "刚刚说过话")
        max_consecutive = self.config.get("chat_limits", {}).get("max_consecutive_replies", 5)
        if self._group_consecutive_replies.get(group_id, 0) >= max_consecutive:
            add(-60, "连续回复太多")

        threshold = cfg.get("followup_threshold", 42) if is_followup else cfg.get("interject_threshold", 68)
        if score < threshold:
            return {
                "should_reply": False, "score": score, "intent": "沉默",
                "reasons": reasons, "need_search": False,
            }

        if is_followup:
            chance = cfg.get("followup_probability", 0.85)
        else:
            base = cfg.get("interject_min_probability", 0.08)
            cap = cfg.get("interject_max_probability", 0.62)
            chance = min(cap, max(base, (score - threshold + 18) / 80))
        if random.random() > chance:
            reasons.append("随机选择继续潜水")
            return {
                "should_reply": False, "score": score, "intent": "沉默",
                "reasons": reasons, "need_search": False,
            }

        intent = self._choose_reply_intent(text, is_followup, is_image_msg, is_pure_sticker)
        return {
            "should_reply": True,
            "score": score,
            "intent": intent,
            "reasons": reasons,
            "need_search": self._should_search_web(text),
        }

    def _looks_like_question(self, text):
        if not text:
            return False
        words = ("吗", "么", "啥", "什么", "怎么", "咋", "为什么", "如何", "谁", "哪里", "哪个", "多少", "有没有", "是不是")
        return "?" in text or "？" in text or any(w in text for w in words)

    def _looks_like_opinion_request(self, text):
        words = ("你觉得", "怎么看", "咋看", "推荐", "建议", "要不要", "能不能", "有没有必要", "值不值")
        return any(w in (text or "") for w in words)

    def _matches_interest_topic(self, text):
        words = (
            "番", "动漫", "漫画", "游戏", "二次元", "gal", "剧情", "角色", "音乐", "歌",
            "电影", "剧", "小说", "梗", "表情包", "电脑", "手机", "ai", "模型", "代码",
        )
        return any(w in (text or "").lower() for w in words)

    def _is_low_signal_text(self, text):
        t = (text or "").strip()
        if not t:
            return True
        if len(t) <= 4 and re.fullmatch(r"[\W_啊哈嘿草笑嗯哦喔呃额]+", t):
            return True
        return t in {"。", "？", "?", "！", "!", "哈哈", "哈哈哈", "草", "笑死", "6", "666"}

    def _choose_reply_intent(self, text, is_followup, is_image_msg, is_pure_sticker=False):
        if is_image_msg and not is_pure_sticker:
            return "评论图片"
        if self._looks_like_question(text):
            return "回答问题"
        if self._looks_like_opinion_request(text):
            return "给出看法"
        if is_followup:
            return "继续闲聊"
        if any(w in text for w in ("笑死", "绷不住", "离谱", "草")):
            return "轻轻吐槽"
        return "自然接话"

    async def _ai_judge_participation(self, group_id, user_id, sender_name, raw_text,
                                       chat_context, is_followup, is_image_msg):
        """Stage 2: Lightweight AI call to decide whether to join conversation.

        Returns ("SKIP" | "REACT" | "JOIN").
        Only called after Stage 1 (local filter) passes.
        SKIP=stay silent, REACT=emoji reaction only, JOIN=full reply.
        Followups skip this stage to save cost (already high confidence).
        """
        # Followups already high confidence — skip AI judgment to save API cost
        if is_followup:
            return "JOIN"

        from .ai import _call_deepseek
        config = self.config

        sys_prompt = (
            "你是小汐的内心判断。看群聊记录，决定要不要说话。\n"
            "判断标准：消息是跟小汐有关吗？小汐了解这个话题吗？现在插话合适吗？\n"
            "SKIP - 跟我无关/不了解/别人在私聊/氛围不适合插话\n"
            "REACT - 跟我有关但不用认真回，发个表情表态就行\n"
            "JOIN - 应该认真回复\n"
            "只回答这三个词之一，不要解释。"
        )

        ctx = chat_context[:800] if chat_context else "（无最近聊天记录）"
        user_prompt = (
            f"【最近群聊】\n{ctx}\n\n"
            f"【当前消息】{sender_name}: {raw_text[:200]}\n\n"
            f"小汐要不要说话？"
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            result = await _call_deepseek(config, messages, max_tokens=5, temperature=0.1)
        except Exception:
            return "SKIP"

        if not result:
            return "SKIP"

        result = result.strip().upper()
        if "JOIN" in result:
            return "JOIN"
        elif "REACT" in result:
            return "REACT"
        return "SKIP"

    _EMOJI_REACTION_MAP = {
        "😂": ["笑死", "哈哈", "好笑", "绷不住", "草", "搞笑"],
        "😭": ["惨", "呜呜", "哭", "太难了", "心疼", "伤心"],
        "👍": ["牛", "厉害", "强", "赞", "666", "确实", "好的"],
        "😱": ["离谱", "震惊", "离谱了", "我靠", "不对劲"],
        "❤️": ["爱", "喜欢", "可爱", "好看", "好美"],
    }

    async def _send_emoji_reaction(self, group_id, message_id, raw_text):
        """Send an emoji reaction (表情表态) on a message based on its content."""
        import random as _random
        emoji_id = "👍"  # default
        for eid, keywords in self._EMOJI_REACTION_MAP.items():
            if any(kw in (raw_text or "") for kw in keywords):
                emoji_id = eid
                break
        try:
            await self.client.set_msg_emoji_like(message_id, emoji_id)
        except Exception:
            pass  # Emoji reaction is best-effort

    async def _check_repeat(self, group_id, raw, sender_user_id):
        cfg = self.config.get("repeat_mode", {})
        if not cfg.get("enabled", True) or len(raw) < 2:
            return False
        # Skip blacklisted users in repeat tracking
        from .guard import is_blacklisted
        if is_blacklisted(group_id, sender_user_id):
            return False
        async with self._lock:
            tracker = self._group_repeat_tracker.setdefault(group_id, {})
            now = time.time()
            entry = tracker.get(raw)
            if entry is None:
                tracker[raw] = (now, {sender_user_id}, 0)
                for k in list(tracker.keys()):
                    if now - tracker[k][0] > 120:
                        del tracker[k]
                return False
            first_ts, users, last_repeat = entry
            if now - first_ts > 120:
                tracker[raw] = (now, {sender_user_id}, 0)
                return False
            users.add(sender_user_id)
            min_users = cfg.get("min_users", 3)
            if len(users) >= min_users:
                cooldown = cfg.get("cooldown_seconds", 60)
                if now - last_repeat < cooldown:
                    return False
                prob = cfg.get("probability", 0.3)
                if random.random() < prob:
                    tracker[raw] = (first_ts, users, now)
                    await self.client.send_group_msg(group_id, raw)
                    return True
            return False

    def _build_chat_context(self, group_id, max_messages=15):
        buffer = list(self._group_msg_buffer.get(group_id, []))
        if not buffer:
            return ''
        recent = buffer[-max_messages:]
        lines = []
        for uid, raw, ts, card in recent:
            if time.time() - ts > 300:
                continue
            clean = raw[:100].replace('\n', ' ')
            lines.append(f'{card}: {clean}')
        return '\n'.join(lines) if lines else ''

    def _check_at_bot(self, message):
        bot_qq = str(self.config["bot_qq"])
        if isinstance(message, str):
            return "[CQ:at,qq=" + bot_qq + "]" in message
        for seg in message:
            if seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == bot_qq:
                return True
        return False

    def _extract_mentions(self, message):
        targets = []
        if isinstance(message, str):
            return targets
        for seg in message:
            if seg.get("type") == "at":
                qq = seg.get("data", {}).get("qq")
                if qq and qq != "all":
                    targets.append(int(qq))
        return targets

    async def _reply(self, group_id, user_id, text):
        # QQ message limit ~4500 chars; split long messages to avoid silent truncation
        max_len = 4000
        if len(text) <= max_len:
            if group_id:
                await self.client.send_group_msg(group_id, text)
            else:
                await self.client.send_private_msg(user_id, text)
            return
        # Split at sentence boundaries when possible
        chunks = []
        remaining = text
        while len(remaining) > max_len:
            split_at = remaining.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                split_at = remaining.rfind("。", 0, max_len)
            if split_at < max_len // 2:
                split_at = remaining.rfind("；", 0, max_len)
            if split_at < max_len // 2:
                split_at = max_len
            chunks.append(remaining[:split_at + 1])
            remaining = remaining[split_at + 1:].lstrip()
        if remaining:
            chunks.append(remaining)
        for chunk in chunks:
            if group_id:
                await self.client.send_group_msg(group_id, chunk)
            else:
                await self.client.send_private_msg(user_id, chunk)
            await asyncio.sleep(0.5)  # Small delay between chunks to avoid rate limits

    def _get_config_path(self):
        return self._config_path
