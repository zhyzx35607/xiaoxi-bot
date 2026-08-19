"""AI conversation memory persistence and compression."""

import asyncio
from collections import deque
import json
import logging
import os
import time

from ..utils import atomic_write_json
from .providers import _call_deepseek

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MEMORY_DIR = os.path.join(_ROOT, "data", "memories")
os.makedirs(MEMORY_DIR, exist_ok=True)

_memories = {}

_memory_timestamps = {}

_last_reply_ts = {}  # context_key -> timestamp, for multi-layer delay

_last_replies_by_user = {}  # user_id -> deque of recent AI replies, for anti-echo

_last_replies_ts = {}  # user_id -> last used timestamp, for cleanup

_REPLIES_CLEANUP_INTERVAL = 3600  # 1 hour

_LAST_REPLY_CLEANUP_TS = 0  # monotonic fallback

_LONG_MEMORY_TASKS = {}  # target key -> running task


def _sanitize_entries(entries):
    """Return valid memory rows with bounded, redacted content."""
    from ..memory import sanitize_for_memory

    sanitized = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        # 缺 content 的条目会在拼 prompt 时 KeyError，直接丢弃（长期记忆条目
        # 只有 ts/content，没有 role，不能按 role 过滤）。
        if "content" not in entry:
            continue
        item = dict(entry)
        item["content"] = sanitize_for_memory(item.get("content", ""))
        sanitized.append(item)
    return sanitized

def _schedule_long_memory(key, coro):
    """Run at most one memory compression task per target at a time."""
    if key in _LONG_MEMORY_TASKS:
        coro.close()
        return None
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        return None
    _LONG_MEMORY_TASKS[key] = task
    def _done(completed):
        _LONG_MEMORY_TASKS.pop(key, None)
        if not completed.cancelled() and completed.exception():
            log.error("Long-memory task failed: %s", completed.exception())
    task.add_done_callback(_done)
    return task

def _memory_file(group_id):
    return os.path.join(MEMORY_DIR, f"group_{group_id}.json")

def _load_memory(group_id, config=None):
    if group_id in _memories:
        _memories[group_id] = _sanitize_entries(_memories[group_id])
        return _memories[group_id]
    path = _memory_file(group_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            data = _sanitize_entries(loaded)
            if data != loaded:
                atomic_write_json(path, data)
            # Clean old entries (config memory_expire_hours, default 72h)
            expire_hours = int((config or {}).get("memory_expire_hours", 72))
            cutoff = now - expire_hours * 3600
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            if len(fresh) < len(data):
                log.info("Memory cleanup: removed %d expired entries for group %s", len(data)-len(fresh), group_id)
            _memories[group_id] = fresh
            _memory_timestamps[group_id] = now
            return fresh
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            log.warning("Group memory load failed: group=%s error=%s", group_id, error)
    _memories[group_id] = []
    _memory_timestamps[group_id] = now
    return _memories[group_id]

def _is_repetitive(user_id, new_reply):
    """Check if new_reply is too similar to recent replies to the same user.
    Returns True if similarity > 0.85 with any of last 5 replies → skip sending.
    """
    # Lazy cleanup on every call
    _cleanup_replies_by_user()
    if user_id not in _last_replies_by_user:
        _last_replies_by_user[user_id] = deque(maxlen=5)
        _last_replies_ts[user_id] = time.time()
        return False
    recent = _last_replies_by_user[user_id]
    if not recent:
        return False
    # Quick exact-match check first
    clean_new = new_reply.strip()
    for old in recent:
        if old.strip() == clean_new:
            return True
    # Slower similarity check
    try:
        import difflib
        for old in recent:
            ratio = difflib.SequenceMatcher(None, old.strip(), clean_new).ratio()
            if ratio > 0.85:
                return True
    except (AttributeError, TypeError, ValueError) as error:
        log.debug("Reply similarity check failed: %s", error)
    return False

def _record_reply(user_id, reply):
    """Record a sent reply for anti-echo tracking."""
    if user_id not in _last_replies_by_user:
        _last_replies_by_user[user_id] = deque(maxlen=5)
        _last_replies_ts[user_id] = time.time()
    _last_replies_by_user[user_id].append(reply.strip())
    _last_replies_ts[user_id] = time.time()

def _cleanup_replies_by_user():
    """Evict _last_replies_by_user entries older than 24 hours.
    Runs lazily every _REPLIES_CLEANUP_INTERVAL seconds."""
    global _LAST_REPLY_CLEANUP_TS
    now = time.time()
    if now - _LAST_REPLY_CLEANUP_TS < _REPLIES_CLEANUP_INTERVAL:
        return
    _LAST_REPLY_CLEANUP_TS = now
    stale = [u for u, ts in _last_replies_ts.items() if now - ts > 86400]
    for u in stale:
        _last_replies_by_user.pop(u, None)
        _last_replies_ts.pop(u, None)
    if stale:
        log.debug("Cleaned up %d stale reply-tracking entries", len(stale))

def _save_memory(group_id, memory, config=None, session=None):
    """Save working memory. Caps at 20, triggers compression to long-term."""
    now = time.time()
    memory[:] = _sanitize_entries(memory)
    for e in memory:
        if "ts" not in e:
            e["ts"] = now
    # Periodic cleanup: evict groups not accessed in > 1 hour
    stale = [g for g, ts in _memory_timestamps.items() if now - ts > 3600]
    for g in stale:
        _memories.pop(g, None)
        _memory_timestamps.pop(g, None)
    if stale:
        log.debug("Memory cleanup: evicted %d stale group caches", len(stale))
    # Cleanup _last_reply_ts: evict entries older than 6 hours
    stale_ts = [k for k, ts in _last_reply_ts.items() if now - ts > 21600]
    for k in stale_ts:
        del _last_reply_ts[k]
    # Cleanup _last_replies_by_user: evict entries older than 6 hours
    stale_reply = [u for u, ts in _last_replies_ts.items() if now - ts > 21600]
    for u in stale_reply:
        _last_replies_by_user.pop(u, None)
        _last_replies_ts.pop(u, None)
    if stale_reply:
        log.debug("Memory cleanup: evicted %d stale reply-tracking entries", len(stale_reply))
    # Cap at 20 entries
    if len(memory) > 20:
        overflow = memory[:len(memory)-20]
        memory = memory[-20:]
        # Trigger bounded async compression.
        runtime = config.get("runtime", {}) if config else {}
        if config and session and overflow and runtime.get("enable_long_memory_compress", False):
            _schedule_long_memory("group:{}".format(group_id),
                                  _compress_to_long_term(group_id, overflow, config, session))
    _memories[group_id] = memory
    _memory_timestamps[group_id] = now
    path = _memory_file(group_id)
    atomic_write_json(path, memory)

def clear_group_memory_cache(group_id):
    """Forget cached group data and cancel pending compression tasks."""
    keys = {group_id, str(group_id)}
    try:
        keys.add(int(group_id))
    except (TypeError, ValueError):
        pass
    for key in keys:
        _memories.pop(key, None)
        _memory_timestamps.pop(key, None)
    task_prefixes = (
        "group:{}".format(group_id),
        "group:{}:u".format(group_id),
    )
    for key, task in list(_LONG_MEMORY_TASKS.items()):
        if key == task_prefixes[0] or key.startswith(task_prefixes[1]):
            task.cancel()


def clear_group_memory(dispatcher, group_id):
    clear_group_memory_cache(group_id)
    path = _memory_file(group_id)
    if os.path.exists(path):
        os.remove(path)

def _user_memory_file(group_id, user_id):
    return os.path.join(MEMORY_DIR, "group_{}_u{}.json".format(group_id, user_id))

def _load_user_memory(group_id, user_id):
    path = _user_memory_file(group_id, user_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            data = _sanitize_entries(loaded)
            if data != loaded:
                atomic_write_json(path, data)
            # 7 day TTL
            cutoff = now - 7 * 86400
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            if fresh != data:
                _save_user_memory(group_id, user_id, fresh, None)
            return fresh
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            log.warning(
                "User memory load failed: group=%s user=%s error=%s",
                group_id, user_id, error,
            )
    return []

def _save_user_memory(group_id, user_id, memory, config=None, session=None, max_entries=None):
    now = time.time()
    memory[:] = _sanitize_entries(memory)
    for e in memory:
        if "ts" not in e:
            e["ts"] = now
    # Cap at user_memory_max from config (default 15); private chat passes 30
    if max_entries is None:
        max_entries = int((config or {}).get("user_memory_max", 15))
    if len(memory) > max_entries:
        overflow = memory[:len(memory) - max_entries]
        memory = memory[-max_entries:]
        # Prefer real LLM compression into long-term memory; fall back to a
        # plain truncation summary when no session/config is available.
        runtime = config.get("runtime", {}) if config else {}
        if (config and session and len(overflow) >= 4
                and runtime.get("enable_long_memory_compress", False)):
            key = ("group:{}:u{}".format(group_id, user_id) if group_id
                   else "private:{}".format(user_id))
            _schedule_long_memory(
                key, _compress_user_to_long(group_id, user_id, overflow, config, session))
        else:
            summary_parts = []
            for e in overflow:
                c = (e.get("content") or "")[:60].replace("\n", " ")
                role = e.get("role", "user")
                summary_parts.append("[{}] {}".format(role, c))
            if summary_parts:
                summary = {"role": "system", "content": "[记忆压缩] " + "; ".join(summary_parts[-4:]), "ts": now}
                memory.insert(0, summary)
                memory = memory[-max_entries:]
    path = _user_memory_file(group_id, user_id)
    atomic_write_json(path, memory)

def clear_user_memory(group_id, user_id):
    path = _user_memory_file(group_id, user_id)
    if os.path.exists(path):
        os.remove(path)

def _long_memory_file(group_id):
    return os.path.join(MEMORY_DIR, "group_{}_long.json".format(group_id))

def _load_long_memory(group_id):
    path = _long_memory_file(group_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            data = _sanitize_entries(loaded)
            if data != loaded:
                atomic_write_json(path, data)
            # 30 day TTL
            cutoff = now - 30 * 86400
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            return fresh
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            log.warning("Long memory load failed: group=%s error=%s", group_id, error)
    return []

def _save_long_memory(group_id, entries):
    path = _long_memory_file(group_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entries = _sanitize_entries(entries)
    # Cap at 10
    if len(entries) > 10:
        entries = entries[-10:]
    atomic_write_json(path, entries)

def _user_long_memory_file(group_id, user_id):
    if group_id:
        return os.path.join(MEMORY_DIR, "group_{}_u{}_long.json".format(group_id, user_id))
    return os.path.join(MEMORY_DIR, "private_{}_long.json".format(user_id))

def _load_user_long_memory(group_id, user_id):
    path = _user_long_memory_file(group_id, user_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            data = _sanitize_entries(loaded)
            if data != loaded:
                atomic_write_json(path, data)
            # 30 day TTL
            cutoff = now - 30 * 86400
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            return fresh
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            log.warning(
                "User long memory load failed: group=%s user=%s error=%s",
                group_id, user_id, error,
            )
    return []

def _save_user_long_memory(group_id, user_id, entries):
    path = _user_long_memory_file(group_id, user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entries = _sanitize_entries(entries)
    if len(entries) > 8:
        entries = entries[-8:]
    atomic_write_json(path, entries)

async def _compress_user_to_long(group_id, user_id, old_entries, config, session):
    """Summarize old per-user chat memory into long-term memory."""
    if not old_entries or len(old_entries) < 4:
        return
    parts = []
    for e in old_entries:
        role = "对方" if e.get("role") == "user" else "小汐"
        c = (e.get("content") or "")[:100].replace("\n", " ")
        parts.append("{}: {}".format(role, c))
    prompt = (
        "将以下对话摘要为1-2句话，用中文，只描述讨论的话题内容，不评价：\n\n"
        + "\n".join(parts[-8:])
    )
    try:
        summary = await _call_deepseek(
            config, [{"role": "user", "content": prompt}],
            max_tokens=80, temperature=0.3, session=session,
        )
        if summary and len(summary) > 5:
            long = _load_user_long_memory(group_id, user_id)
            long.append({"ts": time.time(), "content": summary})
            _save_user_long_memory(group_id, user_id, long)
            log.info(
                "User long-term memory saved group=%s user=%s chars=%s",
                group_id, user_id, len(summary),
            )
    except Exception as e:
        log.error("User long-term compression failed: %s", e)

async def _compress_to_long_term(group_id, old_entries, config, session):
    # Summarize old working memory into long-term memory
    if not old_entries or len(old_entries) < 4:
        return
    parts = []
    for e in old_entries:
        role = "群友" if e.get("role") == "user" else "小汐"
        c = (e.get("content") or "")[:100].replace("\n", " ")
        parts.append("{}: {}".format(role, c))
    
    prompt = (
        "将以下群聊对话摘要为1-2句话，用中文，只描述讨论的话题内容，不评价：\n\n"
        + "\n".join(parts[-8:])
    )
    try:
        summary = await _call_deepseek(
            config, [{"role": "user", "content": prompt}],
            max_tokens=80, temperature=0.3, session=session,
        )
        if summary and len(summary) > 5:
            long = _load_long_memory(group_id)
            long.append({"ts": time.time(), "content": summary})
            _save_long_memory(group_id, long)
            log.info("Long-term memory saved for group=%s chars=%s", group_id, len(summary))
    except Exception as e:
        log.error("Long-term compression failed: %s", e)
