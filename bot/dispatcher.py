# bot/dispatcher.py - Fast message dispatcher with permission system
import asyncio, heapq, json, logging, os, random, re, time

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
chat_log = logging.getLogger("qqbot.chat")


def _private_chat_allowed(dispatcher, user_id):
    """Return whether a private-message sender may enter any bot pipeline."""
    pc_cfg = dispatcher.config.get("private_chat", {})
    if user_id == dispatcher.config.get("bot_owner"):
        return True
    allowed_users = {
        int(value) for value in pc_cfg.get("allowed_users", [])
        if str(value).isdigit()
    }
    return bool(pc_cfg.get("enabled", False) or user_id in allowed_users)


def _disabled_group_activation_allowed(dispatcher, event):
    """Allow only the owner/bot account to recover a disabled group in place."""
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return False
    if event.get("user_id") not in {
        dispatcher.config.get("bot_owner"), dispatcher.config.get("bot_qq")
    }:
        return False
    prefix = dispatcher.config.get("command_prefix", "/")
    return str(event.get("raw_message") or "").strip().lower() == prefix + "enable"


def _event_scope_allowed(dispatcher, event):
    """Hard scope gate applied before parsing, logging, caching, or AI work."""
    group_id = event.get("group_id")
    if group_id and not is_group_enabled(dispatcher, group_id):
        return _disabled_group_activation_allowed(dispatcher, event)
    if (event.get("post_type") in ("message", "message_sent")
            and event.get("message_type") == "private"):
        return _private_chat_allowed(dispatcher, event.get("user_id", 0))
    return True


def _log_chat_message(dispatcher, direction, raw, group_id=None, user_id=0, sender_name=""):
    """Write bounded chat history only for explicitly permitted scopes."""
    if group_id and not is_group_enabled(dispatcher, group_id):
        return False
    if not group_id and not _private_chat_allowed(dispatcher, user_id):
        return False
    text = str(raw or "").replace("\r", "\\r").replace("\n", "\\n")[:500]
    if group_id:
        chat_log.info("%s group=%s user=%s name=%s text=%s",
                      direction, group_id, user_id, sender_name, text)
    else:
        chat_log.info("%s user=%s name=%s text=%s",
                      direction, user_id, sender_name, text)
    return True


def _cq_unescape(text):
    """Undo CQ-code entity escaping (&#91; &#93; &#44; &amp;).

    NapCat puts share cards inline into raw_message as [CQ:json,data=...],
    where the JSON payload is entity-escaped and URLs use \\/ sequences."""
    return (text.replace("&#91;", "[").replace("&#93;", "]")
            .replace("&#44;", ",").replace("&amp;", "&"))


def _share_card_text(message):
    """Pull searchable text out of QQ share-card (json) segments.

    NapCat leaves raw_message empty for pure card messages; without this the
    dispatcher never sees Bilibili links shared as cards."""
    texts = []
    for seg in message or []:
        if not isinstance(seg, dict) or seg.get("type") != "json":
            continue
        data = seg.get("data") or {}
        payload = data.get("data")
        if isinstance(payload, str):
            texts.append(payload.replace("\\/", "/"))
    return "\n".join(texts)


def _read_tail_text(path, line_count=30, max_bytes=65536, max_chars=4000):
    """Read a small tail window without loading the whole rotating log."""
    line_count = max(1, min(int(line_count), 200))
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks = []
            total = 0
            newline_count = 0
            while position > 0 and total < max_bytes and newline_count <= line_count:
                size = min(4096, position, max_bytes - total)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                total += len(chunk)
                newline_count += chunk.count(b"\n")
        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-line_count:])[-max_chars:]
    except FileNotFoundError:
        return ""


async def _service_state(service_name):
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "is-active", service_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return "timeout"
    return stdout.decode("utf-8", errors="replace").strip() or "unknown"


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
        self._group_last_at_bot = {}
        self._group_last_name_reply = {}
        self._user_last_name_reply = {}  # per-user name mention cooldown
        self._group_last_reply_to = {}  # follow-up tracking
        self._group_interject_ts = {}  # last interjection timestamp per group
        self._seen_msg_ids = {}  # message_id -> timestamp
        self._seen_msg_ids_maxlen = 2000
        self._daily_likes = {}
        self._daily_fortunes = {}
        self._state_path = os.path.join(_ROOT, "data", "runtime_state.json")
        self._state_dirty = False
        self._last_state_save = 0
        self._message_stat_updates = 0
        self._scheduler_task = None
        self._bili_push_task = None
        self._rss_guard_task = None
        self._group_reply_timestamps = {}  # rate limit: group_id -> deque of timestamps
        # Chat limits tracking
        self._group_consecutive_replies = {}  # group_id -> int
        self._group_member_cache = {}  # group_id -> {nickname: qq_id}
        self._member_cache_ts = {}  # group_id -> timestamp
        self._private_processing = {}  # user_id -> timestamp; key presence = processing in-flight
        self._private_consecutive_replies = {}  # user_id -> int; track consecutive bot replies
        self._private_last_reply_ts = {}  # user_id -> timestamp; cooldown between replies
        self._private_urgent_pings = {}  # user_id -> [timestamps]; fast messages during cooldown
        self._friend_refresh_lock = asyncio.Lock()
        self._friend_retry_after = 0.0
        runtime = config.get("runtime", {})
        self._max_background_tasks = int(runtime.get("max_background_tasks", 16))
        self._background_tasks = set()
        self._web_search_cache = {}
        self._search_sem = asyncio.Semaphore(max(1, int(runtime.get("search_concurrency", 1))))
        self._group_conversation_state = defaultdict(self._new_conversation_state)
        # Delayed reply queue (group interjections only, lightweight heapq worker)
        self._delayed_queue = []  # heap of [fire_ts, group_id, user_id, message_id, message, raw, sender_card]
        self._delayed_queue_index = {}  # (group_id, user_id) -> active entry for merge
        self._delayed_queue_cap = 20
        self._delayed_queue_event = asyncio.Event()
        self._delayed_worker_task = None
        # Global safety valve: total bot replies across all groups
        self._global_reply_timestamps = deque()
        self._max_global_replies_per_window = 50
        self._global_rate_window = 1800  # 30 min
        # Lightweight stats for reply/skip ratio (logged during save cycle)
        self._ai_outcome_stats = {}  # group_id -> {"reply": int, "skip": int}
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
        # Log per-group reply/skip ratio for observability
        for gid, st in list(self._ai_outcome_stats.items()):
            total = st.get("reply", 0) + st.get("skip", 0)
            if total:
                log.info("Group %s AI outcome: reply=%d skip=%d ratio=%.2f",
                         gid, st["reply"], st["skip"], st["reply"] / total)
        self._ai_outcome_stats.clear()

        # Periodic cleanup of stale state (runs with save cycle, no extra timer needed)
        self._cleanup_stale_state()

    def _cleanup_stale_state(self):
        """Purge data for disabled groups and expired entries to prevent unbounded growth.
        Also evicts seen_msg_ids older than 30 minutes and caps memory cache.
        """
        now = time.time()
        groups_cfg = self.config.get("groups", {})
        enabled_gids = {gid for gid, cfg in groups_cfg.items() if cfg.get("enabled", False)}

        # --- A: Remove data for disabled/non-existent groups ---
        all_tracked_gids = set()
        for src in (self._group_msg_counts, self._group_msg_buffer, self._group_repeat_tracker,
                     self._group_last_at_bot, self._group_last_name_reply,
                     self._group_last_reply_to, self._group_interject_ts,
                     self._group_reply_timestamps, self._group_consecutive_replies,
                     self._group_member_cache, self._member_cache_ts,
                     self._group_conversation_state):
            all_tracked_gids.update(str(k) for k in list(src.keys()))

        stale_gids = all_tracked_gids - enabled_gids
        if stale_gids:
            for gid in stale_gids:
                gid_int = int(gid) if gid.lstrip("-").isdigit() else None
                self._group_msg_counts.pop(gid_int, None)
                self._group_msg_buffer.pop(gid_int, None)
                self._group_repeat_tracker.pop(gid_int, None)
                self._group_last_at_bot.pop(gid_int, None)
                self._group_last_name_reply.pop(gid_int, None)
                self._group_last_reply_to.pop(gid_int, None)
                self._group_interject_ts.pop(gid_int, None)
                self._group_reply_timestamps.pop(gid_int, None)
                self._group_consecutive_replies.pop(gid_int, None)
                self._group_member_cache.pop(gid_int, None)
                self._member_cache_ts.pop(gid_int, None)
                self._group_conversation_state.pop(gid_int, None)
            log.info("Cleaned up %d disabled/non-existent groups from runtime state", len(stale_gids))

        # Drop delayed-reply entries for disabled groups
        if self._delayed_queue:
            kept = []
            for entry in self._delayed_queue:
                if str(entry[1]) in enabled_gids:
                    kept.append(entry)
                else:
                    self._delayed_queue_index.pop((entry[1], entry[2]), None)
            self._delayed_queue = kept
            heapq.heapify(self._delayed_queue)

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

        # Global reply rate safety valve: evict expired timestamps
        while self._global_reply_timestamps and now - self._global_reply_timestamps[0] > self._global_rate_window:
            self._global_reply_timestamps.popleft()

        # _daily_likes / _daily_fortunes: remove non-today keys from memory
        today = time.strftime("%Y%m%d")
        for dct in (self._daily_likes, self._daily_fortunes):
            stale_keys = [k for k in dct if not k.startswith(today + ":")]
            for k in stale_keys:
                del dct[k]

        # _private_processing entries are removed in _handle_private_ai_chat's
        # finally block when processing finishes — no time-based eviction here,
        # so a slow AI call can never look "expired" mid-flight.

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

        # _seen_msg_ids: evict entries older than 30 minutes
        if hasattr(self, "_seen_msg_ids") and len(self._seen_msg_ids) > 500:
            cutoff = now - 1800  # 30 min
            stale_mids = [mid for mid, ts in list(self._seen_msg_ids.items()) if ts < cutoff]
            for mid in stale_mids:
                del self._seen_msg_ids[mid]

        # _web_search_cache: evict entries older than 30 min
        if hasattr(self, "_web_search_cache"):
            stale_ws = [k for k, v in self._web_search_cache.items()
                       if isinstance(v, dict) and now - v.get("ts", 0) > 1800]
            for k in stale_ws:
                self._web_search_cache.pop(k, None)

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

    def start_bili_push(self):
        """Start the UP主 new-video polling loop (idles when nothing watched)."""
        if self._bili_push_task is None:
            from .bilibili import push_loop
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
        import threading, faulthandler
        try:
            self._rss_watch_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._rss_watch_loop = None
        def _watch():
            last = 0.0
            while True:
                time.sleep(1)
                try:
                    kb = self._read_rss_kb()
                    if not kb:
                        continue
                    mb = kb / 1024.0
                    if mb >= 150 and time.time() - last > 20:
                        last = time.time()
                        log.warning("RSS thread-watch: %.0fMB | GC: %s",
                                    mb, self._gc_type_histogram())
                        with open("/tmp/stack_dump.txt", "a") as f:
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
                except Exception:
                    pass
        t = threading.Thread(target=_watch, daemon=True, name="rss-watch")
        t.start()

    async def stop_rss_guard(self):
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
            if not _event_scope_allowed(self, event):
                return
            pt = event.get("post_type", "")
            if pt == "message":
                await self._handle_message(event)
            elif pt == "message_sent":
                # Bot's own outgoing messages — feed into context buffer
                # so AI knows what it just said (self-awareness)
                await self._handle_self_message(event)
            elif pt == "notice":
                from .notice_handler import handle_notice
                await handle_notice(self, event)
            elif pt == "request":
                from .request_handler import handle_request
                await handle_request(self, event)
        except Exception as e:
            log.error("Dispatch error: %s", e, exc_info=True)

    async def _handle_self_message(self, event):
        """Handle message_sent events — bot's own outgoing messages.

        Feeds them into the group/private message buffer so AI
        context includes what the bot itself just said.
        """
        msg_type = event.get("message_type", "")
        group_id = event.get("group_id", 0)
        user_id = event.get("user_id", 0)
        raw = event.get("raw_message", "") or ""
        message = event.get("message", [])
        sender = event.get("sender", {})
        sender_card = sender.get("card") or sender.get("nickname", "小汐")

        if msg_type == "group" and group_id and raw:
            if not is_group_enabled(self, group_id):
                return
            _log_chat_message(
                self, "GROUP_OUT", raw, group_id=group_id,
                user_id=self.config.get("bot_qq", 0), sender_name=sender_card,
            )
            bot_qq = self.config.get("bot_qq", 0)
            # Store in buffer so _build_chat_context sees it. Skip if ai.py just
            # appended the same reply (avoid duplicates from message_sent echo).
            now = time.time()
            buf = self._group_msg_buffer[group_id]
            if not (buf and buf[-1][0] == bot_qq and buf[-1][1][:60] == raw[:60]
                    and now - buf[-1][2] < 10):
                buf.append((bot_qq, raw, now, sender_card))
            # Dedup self-messages
            message_id = event.get("message_id", 0)
            if message_id:
                self._seen_msg_ids[message_id] = time.time()
            log.debug("[SELF] group=%s said: %s", group_id, raw[:60])
            # Fixed commands and title requests sent from the bot account itself
            await self._handle_self_group_command(group_id, raw, message, sender_card)

        elif msg_type == "private" and raw:
            peer_id = event.get("target_id") or user_id
            _log_chat_message(
                self, "PRIVATE_OUT", raw,
                user_id=peer_id, sender_name=sender_card,
            )
            log.debug("[SELF] private said: %s", raw[:60])
            # Commands typed from the bot account in the owner's chat window run
            # as owner commands; replies land in the same window.
            if peer_id == self.config.get("bot_owner"):
                prefix = self.config.get("command_prefix", "/")
                clean = re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
                if clean.startswith(prefix):
                    parts = clean[len(prefix):].split(maxsplit=1)
                    await self._handle_owner_command(
                        parts[0].lower(), parts[1] if len(parts) > 1 else "",
                        peer_id, {"nickname": sender_card}, message, raw,
                    )

    async def _handle_self_group_command(self, group_id, raw, message, sender_card):
        """Run fixed commands / title requests sent from the bot account itself.

        The bot account has master level (see permission.get_user_level), so
        permission checks stay centralized in _run_command.
        """
        prefix = self.config.get("command_prefix", "/")
        bot_qq = self.config.get("bot_qq", 0)
        clean = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        if clean.startswith(prefix):
            parts = clean[len(prefix):].split(maxsplit=1)
            cmd = parts[0].lower()
            if cmd in self.commands:
                log.info("[SELF] running command %s in group %s", cmd, group_id)
                await self._run_command(
                    cmd, parts[1] if len(parts) > 1 else "",
                    group_id, bot_qq, "owner", sender_card, message,
                )
            return
        from .natural_triggers import extract_title_request
        title = extract_title_request(clean)
        if title:
            await self._run_command("mytitle", title, group_id, bot_qq, "owner",
                                    sender_card, message)

    async def _handle_message(self, event):
        msg_type = event.get("message_type", "")
        group_id = event.get("group_id", None)
        user_id = event.get("user_id", 0)

        # Defense in depth: keep direct callers from bypassing dispatch().
        if not _event_scope_allowed(self, event):
            return

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
                    # Evict entries older than 10 minutes to keep recent-only
                    cutoff_ts = time.time() - 600
                    for mid in list(self._seen_msg_ids.keys()):
                        if self._seen_msg_ids[mid] < cutoff_ts:
                            del self._seen_msg_ids[mid]

        # Sender role from NapCat (provided in real-time with each message)
        sender_role = sender.get("role", "member")
        sender_card = sender.get("card") or sender.get("nickname", str(user_id))

        # Group message handling. QQ share cards arrive with an empty
        # raw_message, so recover searchable text from json segments.
        if msg_type == "group" and not raw:
            card_text = _share_card_text(message)
            if ("b23.tv" in card_text or "bilibili.com/video" in card_text
                    or "BV1" in card_text):
                raw = card_text
        # NapCat may instead deliver cards inline as [CQ:json,data=...] in
        # raw_message; unescape so URL/BV detection sees the real links.
        if msg_type == "group" and "[CQ:json,data=" in raw:
            raw = _cq_unescape(raw).replace("\\/", "/")
        if msg_type == "group" and raw:
            group_enabled = is_group_enabled(self, group_id)
            if group_enabled:
                _log_chat_message(
                    self, "GROUP_IN", raw, group_id=group_id,
                    user_id=user_id, sender_name=sender_card,
                )
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

            if not group_enabled:
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
            _log_chat_message(
                self, "PRIVATE_IN", raw,
                user_id=user_id, sender_name=sender_card,
            )
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

        # B站 video share: auto parse + download (feature-gated, 30s cooldown)
        bili_cfg = self.config.get("bilibili", {})
        if (bili_cfg.get("parse_enabled", True)
                and feats.get("bili_parse", True)
                and ("BV1" in raw or "b23.tv" in raw
                     or "bilibili.com/video" in raw)):
            from event_policy import allow_event
            if allow_event("bili_parse", group_id, 30):
                from .bilibili import handle_share
                try:
                    if await handle_share(self, group_id, raw):
                        return
                except Exception as e:
                    log.warning("bili share handle failed: %s", e)

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
            elif cmd_name == "mytitle":
                await self._run_command("mytitle", trig_args.get("title", ""),
                                        group_id, user_id, sender_role, sender_card, message)
            elif cmd_name in ("like", "fortune", "rank", "精华"):
                await self._run_command(cmd_name, "", group_id, user_id, sender_role, sender_card, message)
            return

        # AI-assisted admin intent: the target must come from a real @ segment.
        # The model only chooses the action/duration; permissions stay in code.
        from event_policy import automation_enabled
        if (is_at_others
                and automation_enabled(self.config, "ai_admin_intent", default=False)
                and await self._maybe_execute_admin_intent(
                    group_id, user_id, sender_role, raw, message)):
            return

        # === NEW AI CHAT LOGIC: hard filters + AI-driven judgment ===
        if not feats.get("ai_chat", True):
            return
        from .ai import handle_ai_chat, search_web, _schedule_state
        is_explicit_trigger = is_at_bot or is_name_mentioned
        text = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        # Hard filters applied to every message
        if is_blacklisted(group_id, user_id):
            return
        if not self._check_global_rate_limit():
            if is_explicit_trigger:
                await self.client.send_group_msg(group_id, "今天回太多 让我歇会")
            return
        # Sleep hours: only explicit triggers wake the bot
        schedule_state, _ = _schedule_state()
        if schedule_state == "sleep" and not is_explicit_trigger:
            return
        # Explicit trigger (@bot / name mention): immediate reply
        if is_explicit_trigger:
            now = time.time()
            nm_cfg = self.config.get("name_mention", {})
            group_cd = nm_cfg.get("cooldown_seconds", 10)
            user_cd = nm_cfg.get("user_cooldown_seconds", 15)
            if now - self._group_last_at_bot.get(group_id, 0) < group_cd:
                return
            if now - self._user_last_name_reply.get(user_id, 0) < user_cd:
                return
            self._group_last_at_bot[group_id] = now
            self._user_last_name_reply[user_id] = now
            self._reset_consecutive_replies(group_id)
            allowed, remaining = self._check_rate_limit(group_id)
            if not allowed:
                await self.client.send_group_msg(group_id, "不行了不行了 刷屏太多 我潜一会 回头聊")
                return
            result = await self._do_ai_reply(
                group_id, user_id, raw, sender_card, message, message_id,
                reply_intent="直接回应",
                rate_warning=self._get_rate_limit_warning(remaining),
            )
            self._record_ai_outcome(group_id, bool(result))
            return
        # Follow-up window (user is replying to our recent message)
        is_followup = self._check_followup(group_id, user_id)
        if is_followup:
            allowed, remaining = self._check_rate_limit(group_id)
            if not allowed:
                return
            result = await self._do_ai_reply(
                group_id, user_id, raw, sender_card, message, message_id,
                reply_intent="继续闲聊",
            )
            self._record_ai_outcome(group_id, bool(result))
            return

        # Interjection candidate: cheap hard filter, then defer to delayed queue
        if not self._is_trivial_for_interjection(text, message):
            runtime = self.config.get("runtime", {})
            last_interject = self._group_interject_ts.get(group_id, 0)
            cooldown = runtime.get("non_explicit_judge_cooldown", 240)
            if time.time() - last_interject >= cooldown:
                max_consecutive = self.config.get("chat_limits", {}).get("max_consecutive_replies", 5)
                if self._group_consecutive_replies.get(group_id, 0) < max_consecutive:
                    await self._enqueue_delayed_reply(group_id, user_id, message_id, message, raw, sender_card)

    async def _handle_owner_private(self, user_id, message, raw, sender, message_id):
        """Handle private messages from bot owner: commands first, then AI chat."""
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

        # Non-command messages from owner → treat as normal AI chat
        await self._handle_private_ai_chat(user_id, message, raw, sender, message_id)

    async def _maybe_execute_admin_intent(self, group_id, actor_id, sender_role, raw, message):
        mentions = self._extract_mentions(message)
        if not mentions:
            return False
        text = re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        if not any(word in text for word in ("踢", "禁言", "解禁", "闭嘴", "放出来")):
            return False
        from .permission import get_user_level, LEVEL_ADMIN
        level, _ = await get_user_level(self, group_id, actor_id, sender_role)
        if actor_id != self.config.get("bot_owner") and level < LEVEL_ADMIN:
            return False
        from .ai import _call_deepseek
        prompt = (
            "把管理员的QQ群管理语句解析为JSON。只允许 action=kick_member、ban_member、"
            "unban_member、none。duration为秒，默认禁言600秒，最大2592000秒。"
            "只输出JSON，不要解释。目标用户由系统提供，不要输出用户号。"
        )
        result = await _call_deepseek(
            self.config,
            [{"role": "system", "content": prompt}, {"role": "user", "content": text[:160]}],
            max_tokens=60, temperature=0.1, session=self.client.session)
        try:
            match = re.search(r"\{.*\}", result or "", re.S)
            payload = json.loads(match.group(0) if match else "{}")
            action = payload.get("action", "none")
            if action == "none":
                return False
            from ai_tools import execute_admin_tool
            tool_result = await execute_admin_tool(self, action, {
                "group_id": group_id, "user_id": mentions[0],
                "duration": payload.get("duration", 600),
            }, actor_id, sender_role)
            if tool_result.get("ok"):
                await self._reply(group_id, actor_id, "处理好了")
            else:
                await self._reply(group_id, actor_id, "没处理成：" + str(tool_result.get("error") or tool_result.get("message", "未知错误")))
            return True
        except Exception as exc:
            log.debug("Admin intent parse failed: %s", exc)
            return False

    async def _handle_owner_command(self, cmd, args, user_id, sender, message, raw):
        """Route owner private commands to handlers."""
        sender_name = sender.get("nickname", str(user_id))

        if cmd == "help":
            groups_list = ", ".join(str(g) for g in self.config.get("groups", {}).keys()) or "无"
            help_text = f"""小汐管理面板

群组: {groups_list}

/status - 查看状态
/AI状态 - 查看 SigmaI 和 DeepSeek 运行状态
/打卡状态 - 查看定时群打卡状态
/打卡测试 <群号> - 手动测试原生群打卡
/list - 查看所有群组数据概览
/log N - 查看最近N条日志 (默认30)
/chatlog N - 查看最近N条聊天日志 (默认30)
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
/私聊AI on/off/allow/deny - 私聊AI开关与开放名单
/AI聊天 群号 on/off - 开关指定群的AI聊天
/安全 status/log - 查看安全功能和日志
/info <QQ号> - 查看任意人资料
/点赞信息 - 查看点赞统计
/积分 - 查看uapis积分额度
/b站推送 add 群号 mid - 盯UP主新投稿（mid=UP主空间网址 space.bilibili.com/ 后的数字，也可贴链接）
/全体 群号 内容 - @全体成员
/acg图 群号 on/off - 每日ACG图推送开关
/热榜推送 群号 on/off - 每日热榜推送开关
/b站解析 群号 on/off - B站自动解析开关
"""
            await self._reply(None, user_id, help_text)

        elif cmd in ("enable", "disable"):
            await self._run_command(cmd, args, None, user_id, "member", sender_name, message)

        elif cmd in ("ai状态", "aistatus"):
            from .ai import format_ai_provider_status
            await self._reply(None, user_id, format_ai_provider_status(self.config))

        elif cmd in ("打卡状态", "checkinstatus"):
            from .scheduler import format_checkin_status
            await self._reply(None, user_id, format_checkin_status(self))

        elif cmd in ("打卡测试", "checkintest"):
            gid = args.strip()
            if not gid.isdigit():
                await self._reply(None, user_id, "用法：/打卡测试 群号")
                return
            from .scheduler import run_manual_checkin
            _ok, result_text = await run_manual_checkin(self, gid)
            await self._reply(None, user_id, result_text)

        elif cmd in ("私聊ai", "privateai"):
            await self._run_command("私聊ai", args, None, user_id, "member", sender_name, message)

        elif cmd in ("积分", "uapi"):
            await self._run_command("积分", args, None, user_id, "member", sender_name, message)

        elif cmd in self._private_group_command_names():
            target_group, rest_args = self._parse_private_group_args(args)
            if not target_group:
                await self._reply(None, user_id, "私聊跨群命令要带群号，比如 /{} 群号 参数".format(cmd))
                return
            await self._run_command(
                cmd, rest_args, target_group, user_id, "member", sender_name, message,
            )

        elif cmd in ("log", "chatlog", "聊天日志"):
            n = 30
            if args.strip():
                try:
                    n = int(args.strip())
                except Exception:
                    pass
            try:
                filename = "chat.log" if cmd in ("chatlog", "聊天日志") else "bot.log"
                log_path = os.path.join(_ROOT, filename)
                text = await asyncio.to_thread(
                    _read_tail_text, log_path, n, 65536, 4000 if filename == "chat.log" else 2000)
                await self._reply(None, user_id, text or "无日志")
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
            try:
                bot_state, napcat_state = await asyncio.gather(
                    _service_state("qqbot.service"),
                    _service_state("napcat.service"),
                )
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
                status = f"NapCat：{_cn_state(napcat_state)}\n"
                status += f"小汐：{_cn_state(bot_state)}\n"
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
                mem = _load_memory(parts2[0], self.config)
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
        """Check if user is a friend of the bot (lazy-load, no periodic refresh).

        Cache populated on first private message only. Never proactively
        refreshes — on low-spec servers with 800+ friends, periodic
        get_friend_list is wasteful.
        """
        now = time.time()
        if not hasattr(self, "_friend_cache"):
            self._friend_cache = set()
            self._friend_cache_ts = 0
        if self._friend_cache and now - self._friend_cache_ts < 3600:
            return user_id in self._friend_cache
        if now < self._friend_retry_after:
            return user_id in self._friend_cache

        async with self._friend_refresh_lock:
            now = time.time()
            if self._friend_cache and now - self._friend_cache_ts < 3600:
                return user_id in self._friend_cache
            if now < self._friend_retry_after:
                return user_id in self._friend_cache
            try:
                result = await self.client.call("get_friend_list", {})
                if result.get("status") == "ok":
                    friends = {
                        int(item.get("user_id", 0))
                        for item in result.get("data", [])
                        if item.get("user_id")
                    }
                    self._friend_cache = friends
                    self._friend_cache_ts = now
                    self._friend_retry_after = 0.0
                    log.info("Friend cache loaded on demand: %d friends", len(friends))
                    return user_id in friends
                log.warning("get_friend_list returned %s", result.get("status", "?"))
            except Exception as e:
                log.warning("get_friend_list failed: %s", e)

            self._friend_retry_after = now + 60
            if self._friend_cache:
                self._friend_cache_ts = now
                log.debug("Friend API failed, using stale cache (%d entries)", len(self._friend_cache))
                return user_id in self._friend_cache
            log.warning("Friend list never loaded, rejecting user %s until retry", user_id)
            return False

    async def _handle_private_ai_chat(self, user_id, message, raw, sender, message_id):
        """AI auto-reply for non-owner private chat. Friends only.

        Minimal code intervention — AI decides everything:
        - Whether to reply (output [SKIP] to skip)
        - What to reply
        - How long the reply should be
        - When to end the conversation

        Code only handles: blacklist, friend check, typing delay, sending.
        """
        import re as _re_priv

        sender_name = sender.get("nickname", str(user_id))

        # === Safety: blacklist ===
        from .guard import is_blacklisted
        if is_blacklisted(0, user_id):
            log.debug("Private chat blocked (blacklisted): %s(%s)", sender_name, user_id)
            return

        # === Private AI gate: master switch + allowlist ===
        # Default OFF. Replies only when globally enabled, or the user is in
        # private_chat.allowed_users, or the user is the bot owner.
        pc_cfg = self.config.get("private_chat", {})
        pc_allowed_users = {int(u) for u in pc_cfg.get("allowed_users", [])
                            if str(u).isdigit()}
        is_allowed_user = user_id in pc_allowed_users
        if (user_id != self.config.get("bot_owner")
                and not pc_cfg.get("enabled", False)
                and not is_allowed_user):
            log.debug("Private AI disabled, ignoring: %s(%s)", sender_name, user_id)
            return

        # === Dedup: prevent concurrent AI calls for same user ===
        now = time.time()
        if user_id in self._private_processing:
            log.debug("Private dedup: user %s(%s) already processing, skipping", sender_name, user_id)
            return
        self._private_processing[user_id] = now
        typing_started = False

        try:
            # === Friend-only gate (silent): non-friends get no response at all ===
            if not await self._is_friend(user_id):
                if not is_allowed_user:
                    log.debug("Private chat skipped (not friend): %s(%s)", sender_name, user_id)
                    return

            # === Strip CQ codes for clean text ===
            clean_raw = _re_priv.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
            has_image = any(seg.get("type") == "image" for seg in message if isinstance(seg, dict))

            # Truly empty (no text + no image) → skip even AI call
            if not clean_raw and not has_image:
                log.debug("Private chat skipped (empty): %s(%s)", sender_name, user_id)
                return

            # Show "typing..." while preparing (friend check / vision / search
            # can take seconds); handle_ai_chat keeps it on during generation.
            try:
                _tr = await self.client.call("set_input_status", {
                    "user_id": user_id, "event_type": 1,
                })
                typing_started = isinstance(_tr, dict) and _tr.get("status") == "ok"
            except Exception:
                pass

            # Build image context (only for non-sticker images)
            from .media import extract_message_context
            img_ctx = await extract_message_context(self, None, message)
            if img_ctx:
                img_ctx = img_ctx[:300]

            # Search web for factual questions
            from .ai import search_web
            search_text = clean_raw[:100]
            web_ctx = await search_web(self, search_text) if self._should_search_web(search_text) else ""

            # Call AI — it decides whether to reply and what to say
            from .ai import handle_ai_chat
            consecutive = self._private_consecutive_replies.get(user_id, 0)
            log.info("Private AI evaluating: %s(%s) img=%s consec=%d",
                     sender_name, user_id, bool(img_ctx), consecutive)
            result = await handle_ai_chat(
                self, None, user_id, clean_raw, sender_name,
                image_context=img_ctx or "",
                message_id=message_id,
                web_search_results=web_ctx,
                reply_intent="直接回应",
                consecutive_replies=consecutive,
                interaction_allowed=True,
            )
            if result is True:
                log.info("Private AI replied to %s(%s)", sender_name, user_id)
                self._private_last_reply_ts[user_id] = time.time()
                self._private_consecutive_replies[user_id] = consecutive + 1
                self._private_urgent_pings.pop(user_id, None)
            elif result is None:
                log.debug("Private AI anti-echo skipped: %s(%s)", sender_name, user_id)
            else:
                log.debug("Private AI chose to skip: %s(%s) (consec=%d)", sender_name, user_id, consecutive)
                # Reset consecutive count when AI skips
                self._private_consecutive_replies.pop(user_id, None)
                # Reset after 10 min gap (handled by _cleanup_stale_state)
        finally:
            self._private_processing.pop(user_id, None)
            if typing_started:
                try:
                    await self.client.call("set_input_status", {
                        "user_id": user_id, "event_type": 0,
                    })
                except Exception:
                    pass


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
            "已读", "history", "禁言列表", "转发", "setgroupavatar", "全体", "acg图", "热榜推送", "b站解析", "b站推送", "ai聊天",
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
        """Check if group has exceeded the 30-min reply cap (default 50). Returns (allowed, remaining)."""
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

    def _check_global_rate_limit(self):
        """Safety valve: cap total bot replies across all groups within 30min."""
        now = time.time()
        while self._global_reply_timestamps and now - self._global_reply_timestamps[0] > self._global_rate_window:
            self._global_reply_timestamps.popleft()
        return len(self._global_reply_timestamps) < self._max_global_replies_per_window

    def _record_global_rate_limit(self):
        """Record a reply timestamp for the global safety valve."""
        self._global_reply_timestamps.append(time.time())

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

    async def _check_repeat(self, group_id, raw, sender_user_id):
        cfg = self.config.get("repeat_mode", {})
        if not cfg.get("enabled", True) or len(raw) < 2 or "[CQ:" in raw:
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

    def _is_trivial_for_interjection(self, text, message):
        """Cheap hard filter for unsolicited interjection candidates.

        Explicit triggers bypass this; delayed-queue items re-check it at fire time.
        """
        import re as _re_trivial
        t = _re_trivial.sub(r"\[CQ:[^\]]+\]", "", text or "").strip()
        if not t or len(t) < 3:
            return True
        if message:
            images = [seg for seg in message if isinstance(seg, dict) and seg.get("type") == "image"]
            if images and all(str(seg.get("data", {}).get("sub_type", "0")) != "0" for seg in images):
                if len(t) < 5:
                    return True
        if t in {"。", "？", "?", "！", "!", "哈哈", "哈哈哈", "草", "笑死", "6", "666"}:
            return True
        if len(t) <= 4 and _re_trivial.fullmatch(r"[\W_啊哈嘿草笑嗯哦喔额]+", t):
            return True
        return False

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
        from .ai import handle_ai_chat, search_web, _schedule_state
        from .guard import is_blacklisted

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

    def _record_ai_outcome(self, group_id, replied):
        """Track whether the AI chose to reply or skip for observability."""
        stats = self._ai_outcome_stats.setdefault(group_id, {"reply": 0, "skip": 0})
        if replied:
            stats["reply"] += 1
        else:
            stats["skip"] += 1

    async def _do_ai_reply(self, group_id, user_id, raw, sender_card, message, message_id,
                         reply_intent="自然接话", rate_warning=""):
        """Common helper for explicit and follow-up AI replies."""
        from .ai import handle_ai_chat, search_web

        await self._refresh_member_cache(group_id)
        import re as _re_clean
        clean_raw = _re_clean.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
        chat_ctx = self._build_chat_context(group_id)
        img_ctx = await self._get_image_context(group_id, message)
        web_ctx = await search_web(self, clean_raw) if self._should_search_web(clean_raw) else ""
        result = await handle_ai_chat(
            self, group_id, user_id, clean_raw, sender_card,
            image_context=img_ctx, chat_context=chat_ctx,
            message_id=message_id, web_search_results=web_ctx,
            reply_intent=reply_intent, rate_warning=rate_warning,
            consecutive_replies=self._group_consecutive_replies.get(group_id, 0),
            interaction_allowed=True,
        )
        if result:
            self._record_bot_reply(group_id, user_id)
            self._record_rate_limit(group_id)
            self._record_global_rate_limit()
            self._group_interject_ts[group_id] = time.time()
            self._group_last_reply_to[(group_id, user_id)] = time.time()
        return result

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

    def append_to_buffer(self, group_id, user_id, raw_message, card):
        """Append a message to the group buffer so _build_chat_context can see it.
        Used by ai.py after sending a bot reply."""
        self._group_msg_buffer[group_id].append((user_id, raw_message, time.time(), card))

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
