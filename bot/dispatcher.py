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
from .events.context import (
    _cq_unescape,
    _disabled_group_activation_allowed,
    _event_scope_allowed,
    _log_chat_message,
    _private_chat_allowed,
    _read_tail_text,
    _service_state,
    _share_card_text,
)
from .events.message import GroupMessageMixin, PrivateMessageMixin
from .events.router import RouterMixin
from .services.delayed_reply import DelayedReplyServiceMixin
from .services.health import HealthServiceMixin
from .services.member_cache import MemberCacheMixin

log = logging.getLogger("qqbot")
chat_log = logging.getLogger("qqbot.chat")


















class Dispatcher(
    RouterMixin,
    GroupMessageMixin,
    PrivateMessageMixin,
    HealthServiceMixin,
    MemberCacheMixin,
    DelayedReplyServiceMixin,
):
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








    @staticmethod

    @staticmethod























    def _load_guard_file(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}




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
