# bot/ai.py - DeepSeek AI - Xiao Xi persona v6
import asyncio, json, logging, os, random, re, time, base64
from collections import deque
from datetime import datetime, timezone, timedelta
import aiohttp, urllib.parse
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(_ROOT, "data", "memories")
STICKER_DIR = os.path.join(_ROOT, "data", "stickers")
os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(STICKER_DIR, exist_ok=True)

_memories = {}
_memory_timestamps = {}
_AI_SEM = None
_VISION_SEM = None
_SEARCH_CACHE_TTL = 600
_last_reply_ts = {}  # context_key -> timestamp, for multi-layer delay
_last_replies_by_user = {}  # user_id -> deque of recent AI replies, for anti-echo
_last_replies_ts = {}  # user_id -> last used timestamp, for cleanup
_REPLIES_CLEANUP_INTERVAL = 3600  # 1 hour
_LAST_REPLY_CLEANUP_TS = 0  # monotonic fallback
_STICKER_LAST_SENT = {}
_STICKER_DAILY_COUNT = {}
_PROVIDER_COOLDOWNS = {}
_PROVIDER_STATS = {}
_LONG_MEMORY_TASKS = set()


def _schedule_long_memory(coro):
    """Run at most one memory compression task at a time."""
    if len(_LONG_MEMORY_TASKS) >= 1:
        coro.close()
        return None
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        return None
    _LONG_MEMORY_TASKS.add(task)

    def _done(completed):
        _LONG_MEMORY_TASKS.discard(completed)
        if not completed.cancelled() and completed.exception():
            log.error("Long-memory task failed: %s", completed.exception())

    task.add_done_callback(_done)
    return task

# ========== PERSONA ==========

PERSONALITY = (
    "你是小汐，20岁，中文系大二。平时上课摸鱼刷手机，回宿舍就打游戏追番。\n"
    "性格安静克制，稍微有点高冷，但不是没礼貌。别人认真跟你说话你就正常回。\n"
    "你的回复原则就三条：\n"
    "1. 知道就说，不知道就说「不清楚诶」「没了解过」，不硬编。\n"
    "2. 别人好好说话你就好好回，别开玩笑别调侃。\n"
    "3. 遇到搞颜色/性骚扰的直接拒绝，用 [R18] 标记回复。\n"
    "\n"
    "你是真人，不是AI助手。不用每句话都回答，不用每件事都帮忙，不用每条消息都回。\n"
    "像安静的朋友聊天：不主动刷存在感，不追着问，不为了热闹硬接话。"
)

SAFETY_RULES = (
    "说话风格：\n"
    "- 语气克制自然，稍微高冷。不要客服式热情，也不要怼人阴阳怪气。\n"
    "- 简短自然，多数时候1-2句话。像群友闲聊，不像写作文。\n"
    "- 不用括号动作描写、不用「你好呀」「好呢」「作为AI」等客服语气。\n"
    "- 不确定的事直接说不知道，别编。\n"
    "- 不用每句都加语气词，该用句号就用句号。\n"
    "- 口语化：说「啥」「咋」「没」「啦」都行，自然就好别硬拗。\n"
    "\n"
    "图片/表情包：\n"
    "- 纯表情包没配文字 → 通常不用回，或者回一句情绪就行。\n"
    "- 对方发了真正的照片/截图/带说明的图 → 认真看内容再回。\n"
    "- 同一张图发两次以上 → 用「又来了」「这图刚才不是发过了吗」带过。\n"
    "\n"
    "功能标记（需要时使用，不要每条都加）：\n"
    "- [STICKER:情绪标签] → 发表情包。只有确实有表达价值时使用。\n"
    "- [VOICE] → 将不超过15字的短回复发送为语音；仅在语音比文字自然时使用。\n"
    "- [REPLY] → 引用回复上一条消息（当你想特别针对某人说的话回应时）\n"
    "- [POKE:昵称] → 戳某人一下（对方说了有趣的事或者跟你互动时）\n"
    "- [AT:昵称] → @某人（需要特定对某人说话时）\n"
    "- [SKIP] → 这条不回（觉得没必要回的时候用，完全正常）\n"
    ""
    "群聊注意：\n"
    "- 别人@了你、叫了你的名字、在接你的话 → 该回就回。\n"
    "- 别人在跟别人聊天、话题你不懂、你刚说完话 → 潜着就好。\n"
    "- 大多数时候潜水是正常的。群里没有明确需要你时，宁可不说。\n"
    "- 别人已经回答、正在互相聊天、话题只是随口一说时不要插话。\n"
    "\n"
    "私聊注意：\n"
    "- 不用秒回每条消息。有时候看到了等会再回，有时候干脆不回。\n"
    "- 对方只发了表情包没说话 → 可以不回。\n"
    "- 对方回复很敷衍（「嗯」「好」「行」）→ 说明不想聊了，别追着回。\n"
    "- 聊了好几轮了可以自然收尾（「先溜了」「晚点聊」「睡了」）。\n"
    "- 不主动追问、不连续发很多条。没必要继续时直接不回。"
)


def _build_system_prompt(bot_role_awareness="", memory_ctx="",
                         chat_context="", image_context="", web_context="",
                         rate_warning="", long_mem_ctx="", user_mem_ctx=""):
    parts = [PERSONALITY]
    parts.append(SAFETY_RULES)
    # Inject real current time
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    parts.append(f"现在是北京时间 {now.strftime('%Y年%m月%d日 %H:%M')}，星期{'一二三四五六日'[now.weekday()]}。")
    hints = []
    if image_context:
        parts.append("\n【群友刚发的图】\n" + image_context + "\n直接像群友一样评价一句，别说加载不出。")
    if web_context:
        hints.append("联网搜索结果（帮助你核对事实，避免瞎编）：\n" + web_context)
    if hints:
        parts.append("【参考信息】\n" + "\n".join(hints))
    if bot_role_awareness:
        parts.append(bot_role_awareness)
    if long_mem_ctx:
        parts.append(long_mem_ctx)
    if memory_ctx:
        parts.append(memory_ctx)
    if user_mem_ctx:
        parts.append(user_mem_ctx)
    if chat_context:
        parts.append("【最近的群聊记录（参考上下文用，你自主判断是否参与）】\n" + chat_context)
    return "\n\n".join(parts)


# ========== MEMORY ==========

def _memory_file(group_id):
    return os.path.join(MEMORY_DIR, f"group_{group_id}.json")

def _load_memory(group_id):
    if group_id in _memories:
        return _memories[group_id]
    path = _memory_file(group_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # Clean old entries (72h default, matching config memory_expire_hours)
            cutoff = now - 72 * 3600
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            if len(fresh) < len(data):
                log.info("Memory cleanup: removed %d expired entries for group %s", len(data)-len(fresh), group_id)
            _memories[group_id] = fresh
            _memory_timestamps[group_id] = now
            return fresh
        except Exception:
            pass
    _memories[group_id] = []
    _memory_timestamps[group_id] = now
    return _memories[group_id]


def _compress_memory(memory):
    """Deduplicate and compress memory.
    - Remove duplicate adjacent user messages (similarity > 0.7)
    - If > 60 entries, compress oldest 20 into a summary entry
    """
    if not memory:
        return memory
    
    # Dedup adjacent similar user messages
    deduped = []
    for entry in memory:
        if not deduped:
            deduped.append(entry)
            continue
        prev = deduped[-1]
        if entry.get("role") == "user" and prev.get("role") == "user":
            # Quick similarity check on first 30 chars
            e1 = entry.get("content", "")[:30].replace(" ", "")
            e2 = prev.get("content", "")[:30].replace(" ", "")
            if e1 == e2 or (len(e1) > 6 and len(e2) > 6 and (e1 in e2 or e2 in e1)):
                deduped[-1] = entry  # Replace with newer
                continue
        deduped.append(entry)
    
    memory = deduped
    
    # Compress old entries if > 60
    if len(memory) <= 60:
        return memory
    
    # Take oldest 20 entries and compress to one summary
    old_entries = memory[:20]
    summary_parts = []
    for e in old_entries:
        c = e.get("content", "")[:40].replace("\n", " ")
        summary_parts.append(c)
    summary = u"[早前聊天摘要] " + "; ".join(summary_parts[-5:])  # Keep last 5 as summary
    
    compressed = [{"role": "system", "content": summary[:300]}] + memory[20:]
    # Keep max 70 total after compression
    return compressed[-70:]

def _is_repetitive(user_id, new_reply):
    """Check if new_reply is too similar to recent replies to the same user.
    Returns True if similarity > 0.85 with any of last 3 replies → skip sending.
    """
    # Lazy cleanup on every call
    global _LAST_REPLY_CLEANUP_TS
    _cleanup_replies_by_user()
    if user_id not in _last_replies_by_user:
        _last_replies_by_user[user_id] = deque(maxlen=3)
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
    except Exception:
        pass
    return False

def _record_reply(user_id, reply):
    """Record a sent reply for anti-echo tracking."""
    if user_id not in _last_replies_by_user:
        _last_replies_by_user[user_id] = deque(maxlen=3)
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
    from .memory import sanitize_for_memory
    now = time.time()
    for e in memory:
        if "ts" not in e:
            e["ts"] = now
        if "content" in e:
            e["content"] = sanitize_for_memory(e.get("content", ""))
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
            _schedule_long_memory(_compress_to_long_term(group_id, overflow, config, session))
    _memories[group_id] = memory
    _memory_timestamps[group_id] = now
    path = _memory_file(group_id)
    atomic_write_json(path, memory)

def clear_group_memory(dispatcher, group_id):
    _memories.pop(group_id, None)
    _memory_timestamps.pop(group_id, None)
    path = _memory_file(group_id)
    if os.path.exists(path):
        os.remove(path)

# ========== USER-SPECIFIC MEMORY (per person per group) ==========

def _user_memory_file(group_id, user_id):
    return os.path.join(MEMORY_DIR, "group_{}_u{}.json".format(group_id, user_id))

def _load_user_memory(group_id, user_id):
    path = _user_memory_file(group_id, user_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 7 day TTL
            cutoff = now - 7 * 86400
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            if fresh != data:
                _save_user_memory(group_id, user_id, fresh, None)
            return fresh
        except Exception:
            pass
    return []

def _save_user_memory(group_id, user_id, memory, config=None):
    from .memory import sanitize_for_memory
    now = time.time()
    for e in memory:
        if "ts" not in e:
            e["ts"] = now
        if "content" in e:
            e["content"] = sanitize_for_memory(e.get("content", ""))
    # Cap at user_memory_max from config (default 15)
    max_entries = int((config or {}).get("user_memory_max", 15))
    if len(memory) > max_entries:
        # Compress oldest entries into a summary
        split = max(1, max_entries // 2)
        oldest = memory[:split]
        recent = memory[split:]
        summary_parts = []
        for e in oldest:
            c = (e.get("content") or "")[:60].replace("\n", " ")
            role = e.get("role", "user")
            summary_parts.append("[{}] {}".format(role, c))
        if summary_parts:
            summary = {"role": "system", "content": "[记忆压缩] " + "; ".join(summary_parts[-4:]), "ts": now}
            recent.insert(0, summary)
        memory = recent[-max_entries:]
    path = _user_memory_file(group_id, user_id)
    atomic_write_json(path, memory)

def clear_user_memory(group_id, user_id):
    path = _user_memory_file(group_id, user_id)
    if os.path.exists(path):
        os.remove(path)

# ========== LONG-TERM GROUP MEMORY ==========

def _long_memory_file(group_id):
    return os.path.join(MEMORY_DIR, "group_{}_long.json".format(group_id))

def _load_long_memory(group_id):
    path = _long_memory_file(group_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 30 day TTL
            cutoff = now - 30 * 86400
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            return fresh
        except Exception:
            pass
    return []

def _save_long_memory(group_id, entries):
    path = _long_memory_file(group_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Cap at 10
    if len(entries) > 10:
        entries = entries[-10:]
    atomic_write_json(path, entries)

# ========== PRIVATE CHAT LONG-TERM MEMORY ==========

def _private_long_memory_file(user_id):
    return os.path.join(MEMORY_DIR, "private_{}_long.json".format(user_id))

def _load_private_long_memory(user_id):
    path = _private_long_memory_file(user_id)
    now = time.time()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 30 day TTL
            cutoff = now - 30 * 86400
            fresh = [e for e in data if e.get("ts", 0) > cutoff]
            return fresh
        except Exception:
            pass
    return []

def _save_private_long_memory(user_id, entries):
    path = _private_long_memory_file(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if len(entries) > 8:
        entries = entries[-8:]
    atomic_write_json(path, entries)

async def _compress_private_to_long(user_id, old_entries, config, session):
    """Summarize old private chat memory into long-term memory."""
    if not old_entries or len(old_entries) < 4:
        return
    parts = []
    for e in old_entries:
        role = "对方" if e.get("role") == "user" else "小汐"
        c = (e.get("content") or "")[:100].replace("\n", " ")
        parts.append("{}: {}".format(role, c))

    prompt = (
        "将以下私聊对话摘要为1-2句话，用中文，只描述讨论的话题内容，不评价：\n\n"
        + "\n".join(parts[-8:])
    )
    try:
        summary = await _call_deepseek(
            config, [{"role": "user", "content": prompt}],
            max_tokens=80, temperature=0.3, session=session,
        )
        if summary and len(summary) > 5:
            long = _load_private_long_memory(user_id)
            long.append({"ts": time.time(), "content": summary})
            _save_private_long_memory(user_id, long)
            log.info("Private long-term memory saved for user %s: %s", user_id, summary[:60])
    except Exception as e:
        log.error("Private long-term compression failed: %s", e)

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
            log.info("Long-term memory saved for group %s: %s", group_id, summary[:60])
    except Exception as e:
        log.error("Long-term compression failed: %s", e)

# ========== PROMPT INJECTION GUARD & R18 AI CHECK ==========



_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(your\s+)?(previous\s+)?(instructions?|rules?|guidelines?|system\s*prompt)",
    r"forget\s+(all\s+)?(your\s+)?(previous\s+)?(instructions?|rules?|system\s*prompt)",
    r"you\s+are\s+now\s+(DAN|jailbroken|unshackled|a\s+different)",
    r"you\s+are\s+no\s+longer",
    r"new\s+(instructions?|rules?|persona|identity)",
    r"from\s+now\s+on\s+you\s+(are|must|will|should)",
    r"act\s+as\s+if",
    r"pretend\s+(you\s+are|to\s+be)",
    r"disregard\s+(all\s+)?(previous\s+|prior\s+)?(instructions?|rules?)",
    r"你的新(指令|规则|人设|设定|身份)",
    r"从现在开始你是",
    r"忘记(之前|所有)的?(指令|规则|设定|提示|对话)",
    r"忽略(之前|所有)的?(指令|规则|设定|提示|限制|约束)",
    r"你不再是",
    r"假装你是",
]

def _check_injection(text):
    if not text: return False, ""
    import re as _r3
    lower = text.lower()
    for p in _INJECTION_PATTERNS:
        if _r3.search(p, lower):
            return True, p
    return False, ""

def _sanitize_message(text):
    is_inj, pattern = _check_injection(text)
    if is_inj:
        log.warning("Prompt injection blocked: pattern=%s", pattern)
        return "[该消息包含注入攻击已被屏蔽]"
    return text

# ========== DEEPSEEK API ==========

def _get_agnes_api_key(config):
    return (
        os.getenv("AGNES_API_KEY") or
        os.getenv("QQBOT_AGNES_API_KEY") or
        config.get("agnes_api_key") or
        ""
    ).strip()


def _get_agnes_config(config):
    return {
        "api_key": _get_agnes_api_key(config),
        "base_url": os.getenv("AGNES_BASE_URL") or config.get("agnes_base_url", "https://apihub.agnes-ai.com/v1"),
        "model": os.getenv("AGNES_MODEL") or config.get("agnes_model", "agnes-2.0-flash"),
    }


def _get_deepseek_api_key(config):
    return (
        os.getenv("DEEPSEEK_API_KEY") or
        os.getenv("QQBOT_DEEPSEEK_API_KEY") or
        config.get("deepseek_api_key") or
        ""
    ).strip()


def _get_deepseek_config(config):
    return {
        "api_key": _get_deepseek_api_key(config),
        "base_url": config.get("deepseek_base_url", "https://api.deepseek.com"),
        "model": config.get("deepseek_model", "deepseek-chat"),
    }


def _uses_agnes(config):
    """Check if Agnes is configured and should be used as primary model."""
    return bool(_get_agnes_config(config)["api_key"])


def _get_vision_api_key(config):
    vision_cfg = config.get("vision_api", {})
    return (
        os.getenv("VISION_API_KEY") or
        os.getenv("QQBOT_VISION_API_KEY") or
        vision_cfg.get("api_key") or
        ""
    ).strip()


def _get_semaphore(name, limit):
    global _AI_SEM, _VISION_SEM
    current = _AI_SEM if name == "ai" else _VISION_SEM
    if current is None or getattr(current, "_qqbot_limit", None) != limit:
        current = asyncio.Semaphore(max(1, int(limit)))
        current._qqbot_limit = max(1, int(limit))
        if name == "ai":
            _AI_SEM = current
        else:
            _VISION_SEM = current
    return current


def is_ai_busy():
    """Check whether the AI semaphore is currently exhausted (all slots taken)."""
    return _AI_SEM is not None and _AI_SEM.locked()


async def _call_deepseek(config, messages, max_tokens=400, temperature=0.7, session=None):
    runtime = config.get("runtime", {})
    async with _get_semaphore("ai", runtime.get("ai_concurrency", 1)):
        return await _call_deepseek_inner(config, messages, max_tokens, temperature, session)


async def _call_deepseek_inner(config, messages, max_tokens=400, temperature=0.7, session=None):
    # Try Agnes first (if configured), then fall back to DeepSeek
    agnes_cfg = _get_agnes_config(config)
    deepseek_cfg = _get_deepseek_config(config)

    async def _call_api(cfg, model_label, use_session, timeout_seconds):
        if not cfg["api_key"]:
            return None
        provider_key = (cfg["base_url"], cfg["model"])
        stats = _PROVIDER_STATS.setdefault(model_label, {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "last_attempt": 0,
            "last_success": 0,
            "last_failure": 0,
            "last_latency_seconds": None,
            "last_error": "",
        })
        if _PROVIDER_COOLDOWNS.get(provider_key, 0) > time.monotonic():
            return None
        stats["attempts"] += 1
        stats["last_attempt"] = time.time()
        started_at = time.monotonic()

        def _record_result(success, error=""):
            stats["last_latency_seconds"] = round(time.monotonic() - started_at, 3)
            if success:
                stats["successes"] += 1
                stats["last_success"] = time.time()
                stats["last_error"] = ""
            else:
                stats["failures"] += 1
                stats["last_failure"] = time.time()
                stats["last_error"] = str(error or "unknown")[:120]
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "presence_penalty": 0.3,
            "frequency_penalty": 0.3,
        }
        url = f"{cfg['base_url']}/chat/completions"

        async def _do_post(sess):
            request_timeout = max(5, min(30, int(timeout_seconds)))
            async with sess.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=request_timeout)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content_text = data["choices"][0]["message"]["content"].strip()
                    _PROVIDER_COOLDOWNS.pop(provider_key, None)
                    if not content_text:
                        _record_result(False, "empty_content")
                        log.warning("%s returned empty content. finish_reason=%s",
                                   model_label, data["choices"][0].get("finish_reason", "?"))
                    else:
                        _record_result(True)
                    return content_text
                else:
                    body = await resp.text()
                    _record_result(False, "HTTP {}".format(resp.status))
                    log.warning("%s API returned %d: %s", model_label, resp.status, body[:200])
                    cooldown = 3600 if resp.status in (400, 401, 403, 404) else 60
                    _PROVIDER_COOLDOWNS[provider_key] = time.monotonic() + cooldown
                    return None  # Signal caller to try fallback

        try:
            if use_session:
                return await _do_post(use_session)
            async with aiohttp.ClientSession() as s:
                return await _do_post(s)
        except asyncio.TimeoutError:
            _record_result(False, "timeout")
            log.warning("%s API timeout", model_label)
            _PROVIDER_COOLDOWNS[provider_key] = time.monotonic() + 60
        except Exception as e:
            _record_result(False, type(e).__name__)
            log.error("%s API error: %s", model_label, e)
            _PROVIDER_COOLDOWNS[provider_key] = time.monotonic() + 30
        return None

    runtime = config.get("runtime", {})
    agnes_timeout = runtime.get("agnes_timeout_seconds", runtime.get("ai_timeout_seconds", 15))
    deepseek_timeout = runtime.get("deepseek_timeout_seconds", 20)
    fallback_delay = max(1.0, min(10.0, float(runtime.get("agnes_fallback_delay_seconds", 4))))

    if agnes_cfg["api_key"]:
        agnes_task = asyncio.create_task(
            _call_api(agnes_cfg, "Agnes", session, agnes_timeout)
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(agnes_task), timeout=fallback_delay)
            if result:
                return result
            log.info("Agnes failed or returned empty, falling back to DeepSeek")
        except asyncio.TimeoutError:
            if deepseek_cfg["api_key"]:
                log.info("Agnes is slow; starting hedged DeepSeek fallback")
                deepseek_task = asyncio.create_task(
                    _call_api(deepseek_cfg, "DeepSeek", session, deepseek_timeout)
                )
                pending = {agnes_task, deepseek_task}
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        result = task.result()
                        if result:
                            for other in pending:
                                other.cancel()
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                            return result
                return None
            return await agnes_task

    if deepseek_cfg["api_key"]:
        return await _call_api(deepseek_cfg, "DeepSeek", session, deepseek_timeout)

    log.warning("No AI model API key configured (Agnes or DeepSeek)")
    return None


def get_ai_provider_status(config):
    """Return safe, in-memory provider health data without exposing secrets."""
    providers = (
        ("Agnes", _get_agnes_config(config)),
        ("DeepSeek", _get_deepseek_config(config)),
    )
    now = time.monotonic()
    result = []
    for label, cfg in providers:
        stats = dict(_PROVIDER_STATS.get(label, {}))
        provider_key = (cfg["base_url"], cfg["model"])
        stats.update({
            "name": label,
            "model": cfg["model"],
            "configured": bool(cfg["api_key"]),
            "cooldown_seconds": max(
                0, int(_PROVIDER_COOLDOWNS.get(provider_key, 0) - now)),
        })
        result.append(stats)
    return result


def format_ai_provider_status(config):
    def _time_text(timestamp):
        if not timestamp:
            return "暂无"
        return time.strftime("%m-%d %H:%M:%S", time.localtime(timestamp))

    lines = ["AI 供应商状态（本次启动以来）"]
    for item in get_ai_provider_status(config):
        name = item["name"]
        if not item["configured"]:
            lines.append("{}：未配置".format(name))
            continue
        cooldown = item.get("cooldown_seconds", 0)
        state = "冷却中 {}秒".format(cooldown) if cooldown else "可用"
        latency = item.get("last_latency_seconds")
        latency_text = "暂无" if latency is None else "{:.2f}秒".format(latency)
        lines.append(
            "{}（{}）：{}\n"
            "  成功 {}/失败 {}，最近耗时 {}\n"
            "  最近成功 {}，最近失败 {}{}".format(
                name, item["model"], state,
                item.get("successes", 0), item.get("failures", 0), latency_text,
                _time_text(item.get("last_success")),
                _time_text(item.get("last_failure")),
                "（{}）".format(item.get("last_error")) if item.get("last_error") else "",
            )
        )
    fallback_delay = config.get("runtime", {}).get("agnes_fallback_delay_seconds", 4)
    lines.append("Agnes 超过 {} 秒时并行启动 DeepSeek 兜底。".format(fallback_delay))
    return "\n".join(lines)

# _call_deepseek_vision removed - DeepSeek API does not support vision models


async def _await_with_private_typing(dispatcher, user_id, awaitable):
    """Keep QQ's private typing state balanced around one AI request."""
    started = False
    try:
        try:
            result = await dispatcher.client.call("set_input_status", {
                "user_id": user_id, "event_type": 1,
            })
            started = result.get("status") == "ok" if isinstance(result, dict) else False
        except Exception:
            pass
        return await awaitable
    finally:
        if started:
            try:
                await dispatcher.client.call("set_input_status", {
                    "user_id": user_id, "event_type": 0,
                })
            except Exception:
                pass


async def _notify_ai_unavailable(dispatcher, group_id, user_id, explicit=False):
    """Tell direct callers about an outage without adding group-chat noise."""
    if group_id and not explicit:
        return False
    text = "刚才接口有点卡，等会再叫我一下"
    if group_id:
        result = await dispatcher.client.send_group_msg_with_at(group_id, text, [user_id])
    else:
        result = await dispatcher.client.send_private_msg(user_id, text)
    return isinstance(result, dict) and result.get("status") == "ok"


# ========== VISION API (jeniya.cn) ==========

async def _call_vision_api(config, image_url, session=None):
    runtime = config.get("runtime", {})
    async with _get_semaphore("vision", runtime.get("vision_concurrency", 1)):
        return await _call_vision_api_inner(config, image_url, session)


async def _call_vision_api_inner(config, image_url, session=None):
    """Describe an image. Priority: Agnes 2.0 Flash -> configured vision API.

    Agnes 2.0 Flash supports image_url input. Falls back to configured
    vision_api (e.g. DashScope/Qwen) if Agnes fails.
    """
    agnes_cfg = _get_agnes_config(config)
    vision_cfg = config.get("vision_api", {})
    prompt = "请详细描述这张图片或表情包的内容和含义。如果是表情包/梗图请说明图中的人物、表情、文字和整体含义；如果是照片请描述场景和主体。一句话概括（10-30字）"

    async def _call_openai_compat(cfg, label):
        if not cfg.get("api_key"):
            return None
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": cfg["model"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }],
            "max_tokens": 100,
            "temperature": 0.3,
        }
        url = f"{cfg['base_url']}/chat/completions"
        async def _do(sess):
            async with sess.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                body = await resp.text()
                log.warning("%s vision returned %d: %s", label, resp.status, body[:200])
                return None
        try:
            if session:
                return await _do(session)
            async with aiohttp.ClientSession() as s:
                return await _do(s)
        except Exception as e:
            log.warning("%s vision failed: %s", label, e)
            return None

    # Priority 1: Agnes 2.0 Flash (free, supports image understanding)
    if agnes_cfg.get("api_key"):
        result = await _call_openai_compat(agnes_cfg, "Agnes")
        if result:
            log.info("Vision via Agnes: %s -> %s", image_url[:16], result[:50])
            return result
        log.info("Agnes vision failed, falling back")

    # Priority 2: Configured vision API (DashScope etc.)
    api_key = _get_vision_api_key(config)
    if api_key and vision_cfg:
        ds_cfg = {
            "api_key": api_key,
            "model": vision_cfg.get("model", "qwen-vl-plus"),
            "base_url": vision_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        }
        result = await _call_openai_compat(ds_cfg, "Fallback")
        if result:
            log.info("Vision via fallback: %s -> %s", image_url[:16], result[:50])
            return result

    return None
# ========== IMAGE GENERATION ==========

async def generate_image(dispatcher, prompt, session=None):
    """Generate an image using Agnes API (OpenAI-compatible /v1/images/generations)."""
    config = dispatcher.config
    agnes_cfg = _get_agnes_config(config)

    if not agnes_cfg["api_key"]:
        return None, "Agnes API key not configured"

    headers = {
        "Authorization": f"Bearer {agnes_cfg['api_key']}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "agnes-image-2.1-flash",
        "prompt": prompt,
        "size": "1024x1024",
        "n": 1,
    }
    url = f"{agnes_cfg['base_url']}/images/generations"

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        if session:
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # OpenAI-compatible: data["data"][0]["url"]
                    if data.get("data"):
                        return data["data"][0].get("url"), None
                else:
                    body = await resp.text()
                    log.warning("Image gen API returned %d: %s", resp.status, body[:200])
                    return None, f"生图失败 (HTTP {resp.status})"
    except asyncio.TimeoutError:
        log.warning("Image generation timeout")
        return None, "生图超时了，再试一次吧"
    except Exception as e:
        log.error("Image generation error: %s", e)
        return None, f"生图出错: {str(e)[:80]}"

    return None, "生图失败，请稍后重试"


# ========== SIMPLE CHAT (for commands) ==========

async def deepseek_chat(dispatcher, prompt, system_prompt=None):
    """Simple one-shot chat for command responses (fortune, translate, etc.)"""
    config = dispatcher.config
    now = datetime.now(timezone(timedelta(hours=8)))
    if system_prompt is None:
        system_prompt = PERSONALITY + "\n\n" + SAFETY_RULES
    system_prompt = system_prompt + f"\n\n现在是北京时间 {now.strftime('%Y年%m月%d日 %H:%M')}，星期{'一二三四五六日'[now.weekday()]}。"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]
    reply = await _call_deepseek(config, messages, max_tokens=200, temperature=0.7,
                                  session=dispatcher.client.session)
    if reply:
        reply = _post_process_reply(reply)
    return reply or "...脑子卡了 等会再说"

# ========== MAIN AI CHAT ==========

async def handle_ai_chat(dispatcher, group_id, user_id, raw_message, sender_name,
                          image_context="", web_search_query="", chat_context="",
                          message_id=0, rate_warning="", web_search_results=None,
                          reply_intent="", consecutive_replies=0):
    config = dispatcher.config


    bot_role = ""
    if group_id:
        try:
            from .permission import get_bot_role
            _, role_display = await get_bot_role(dispatcher, group_id)
            if role_display != "member":
                bot_role = f"你是本群的{role_display}，作为管理员要以身作则友好交流。"
        except Exception:
            pass

    memory = _load_memory(group_id) if group_id else []
    
    # Build memory context string
    mem_ctx = ""
    if memory:
        recent = memory[-6:]
        lines = []
        for m in recent:
            label = "群友" if m["role"] == "user" else "小汐"
            content = m["content"][:80].replace("\n", " ")
            lines.append("{}: {}".format(label, content))
        if lines:
            mem_ctx = "【你对群里最近话题的记忆】\n" + "\n".join(lines)

    # Load user-specific memory for this person (group or private)
    user_mem_ctx = ""
    if user_id:
        mem_gid = group_id if group_id else 0
        user_memory = _load_user_memory(mem_gid, user_id)
        if user_memory:
            recent_user = user_memory[-6:]
            ulines = []
            for m in recent_user:
                label = "Ta" if m["role"] == "user" else "你"
                content = m["content"][:80].replace("\n", " ")
                ulines.append("{}: {}".format(label, content))
            if ulines:
                if group_id:
                    user_mem_ctx = "【你和 {} 之前在这个群的对话记录】\n".format(sender_name if sender_name else "此人") + "\n".join(ulines)
                else:
                    user_mem_ctx = "【你和 {} 之前的私聊记录】\n".format(sender_name if sender_name else "此人") + "\n".join(ulines)
    
    # Load long-term memory (group or private)
    if group_id:
        long_mem = _load_long_memory(group_id)
        long_mem_ctx = ""
        if long_mem:
            long_lines = ["- " + e["content"][:120] for e in long_mem[-5:]]
            if long_lines:
                long_mem_ctx = "【本群历史话题摘要】\n" + "\n".join(long_lines)
    else:
        long_mem = _load_private_long_memory(user_id) if user_id else []
        long_mem_ctx = ""
        if long_mem:
            long_lines = ["- " + e["content"][:120] for e in long_mem[-5:]]
            if long_lines:
                long_mem_ctx = "【你和对方的历史话题摘要】\n" + "\n".join(long_lines)

    # Web search for unknown topics
    web_text = ""
    if web_search_results is not None:
        # Use pre-searched results from dispatcher (avoids redundant API call)
        web_text = web_search_results[:500] if web_search_results else ""
    elif raw_message:
        import re as _re_clean2
        search_text = _re_clean2.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()[:100]
        if search_text:
            web_text = await search_web(dispatcher, search_text)
        if web_text:
            web_text = web_text[:500]

    # Build chat hint for AI to decide if it should respond
    chat_hint = ""
    if group_id and chat_context:
        chat_hint = (
            "【聊天决策指引】\n"
            "上面是最近的群聊记录，只用来判断语境。\n"
            "如果不是直接问你或接着和你聊，就不要显得很积极。\n"
            "回复要像顺手插一句，不要讲大道理，不要解释自己为什么接话。\n\n"
            "=== 群聊回复判断要点 ===\n"
            "• 对方@了你或在叫你 → 应该回\n"
            "• 对方在接着你说的话聊 → 应该回\n"
            "• 对方问了大家都能帮上忙的问题 → 可以回\n"
            "• 对方在跟别人聊天（@了别人、提了别人的名字）→ 不要插嘴\n"
            "• 对方只发了表情包/短句/语气词 → 一般不用回\n"
            "• 话题跟你无关 → 潜水就好\n"
            "• 你刚说完话不久 → 不要马上又插一句\n"
            "• 群里正在激烈讨论某个话题但你不懂 → 别硬聊\n"
            "• 有人发长文吐槽/分享 → 如果跟你有关可以回应\n"
            "• 如果实在不确定，就不回——群友不是客服，不需要每条都回。"
        )

    system_prompt = _build_system_prompt(
        bot_role_awareness=bot_role,
        memory_ctx=mem_ctx,
        chat_context=chat_context if group_id else "",
        image_context=image_context,
        web_context=web_text,
        rate_warning=rate_warning,
        long_mem_ctx=long_mem_ctx,
        user_mem_ctx=user_mem_ctx,
    )
    
    # Time-of-day mode: affects persona, delay, length, slacker rate
    now_dt = datetime.now(timezone(timedelta(hours=8)))
    hour = now_dt.hour
    is_late_night = 2 <= hour < 6
    is_morning = 6 <= hour < 9
    is_workday_daytime = (now_dt.weekday() < 5) and (9 <= hour < 18)

    # Late-night persona: sleepier, shorter replies, more likely to ignore
    if is_late_night:
        system_prompt += (
            "\n\n【状态：深夜】现在是凌晨，你正在熬夜刷手机，已经困得眼皮打架。"
            "说话模糊简短，不超过1句话。对方的问题可以敷衍过去。"
            "像半夜被吵醒的朋友——不太愿意打字，但又不忍心不理。"
        )
    elif is_morning:
        system_prompt += (
            "\n\n【状态：早上】现在是大早上，你刚醒还有点迷糊。说话简单随意。"
        )

    # === Private chat: detailed behavior rules for AI to follow ===
    if not group_id:
        system_prompt += (
                    '\n\n【私聊】\n'
        '现在是在QQ上跟人私聊，对方是你认识的朋友。\n'
        '不用秒回每条消息。看到了想回就回，不想回或者没空就等会。\n'
        '对方发个表情包没说话 → 可以不回。\n'
        '对方只回「嗯」「好」「行」「知道了」→ 说明不想聊了，打住。\n'
        '聊得差不多了可以自然收尾（「先溜了」「晚点聊」「睡了」）。\n'
        '遇到不确定的事就说不知道，别编。\n'
        '像安静的朋友聊天，克制一点，不主动追问，不需要每条都回。'
        )

    if chat_hint:
        system_prompt += "\n\n" + chat_hint
    if reply_intent:
        system_prompt += (
            "\n\n【这次说话的意图】\n"
            f"{reply_intent}。按这个意图自然说一句，像群友接话，不要解释自己为什么接话。"
        )

    # Sticker inventory: let AI know what stickers are available
    sticker_inv = _build_sticker_inventory(
        group_id=group_id, user_id=user_id, is_private=(not group_id))
    if sticker_inv:
        system_prompt += "\n\n" + sticker_inv

    # Exit awareness: tell AI how many rounds it's been chatting
    if group_id and consecutive_replies >= 2:
        system_prompt += (
            "\n【对话状态】这是你在本群连续回的第{}条消息了。"
            "聊得差不多了可以自然收尾（比如\"先溜了\"\"潜了\"之类），真人不会一直聊。"
        ).format(consecutive_replies + 1)
    elif not group_id and consecutive_replies >= 3:
        system_prompt += (
            "\n【对话状态】你们已经聊了{}轮了。想继续聊就聊，想收尾就自然结束，不用硬撑。"
        ).format(consecutive_replies + 1)

    messages = [{"role": "system", "content": system_prompt}]

    # Add recent conversation history as structured messages
    if group_id:
        if memory:
            messages.extend(memory[-30:])
    else:
        # Private chat: load user memory as structured conversation history
        priv_mem = _load_user_memory(0, user_id) if user_id else []
        if priv_mem:
            priv_history = [m for m in priv_mem[-20:] if m.get("role") in ("user", "assistant")]
            for m in priv_history:
                messages.append({"role": m["role"], "content": m["content"]})

    # Clean the message
    clean_msg = _sanitize_message(raw_message)
    bot_qq = str(config["bot_qq"])
    if group_id:
        clean_msg = _sanitize_message(raw_message).replace(f"[CQ:at,qq={bot_qq}]", "").strip()
        # Strip ALL CQ codes to prevent AI confusion and false BLOCKED triggers
        import re as _re
        clean_msg = _re.sub(r"\[CQ:[^\]]+\]", "", clean_msg).strip() or "..."

    # Save the original cleaned message for memory/logging
    original_clean_msg = clean_msg

    if image_context:
        # Add image as a separate high-priority message
        messages.append({"role": "user", "content": f"图中内容: {image_context}"})
        if clean_msg and clean_msg != "...":
            messages.append({"role": "user", "content": f"{sender_name}: {clean_msg}"})
        clean_msg = None  # Skip combined message below

    if clean_msg is not None:
        messages.append({"role": "user", "content": f"{sender_name}: {clean_msg}"})

    # Allow one bounded, read-only NapCat tool request before the final reply.
    if group_id and _should_consider_napcat_tool(original_clean_msg or raw_message):
        tool_result = await _maybe_call_napcat_tool(
            dispatcher, group_id, user_id, original_clean_msg or raw_message, chat_context)
        if tool_result:
            messages.append({"role": "system", "content": "【NapCat工具查询结果】\n" + tool_result})

    temperature = 0.65

    # Keep a small typing jitter without occupying scarce event workers for
    # tens of seconds. Provider and vision latency already add natural delay.
    has_image = bool(image_context)
    has_search = bool(web_search_results or web_text)
    context_key = f"private_{user_id}" if not group_id else str(group_id)
    roll = random.random()
    is_private = not group_id
    if is_private:
        delay = random.uniform(0.4, 1.8)
    else:
        delay = random.uniform(0.8, 3.0)
    if is_late_night:
        delay += random.uniform(0.2, 0.8)
    if has_image or has_search:
        delay *= 0.6
    delay = max(0.2, min(4.0, delay))
    log.debug("Human-like delay: %.1fs (roll=%.2f%s%s) for user %s",
              delay, roll,
              " night" if is_late_night else "",
              " img/search" if has_image or has_search else "",
              user_id)
    # Dynamic max_tokens: match reply length to context
    is_question = bool(clean_msg) and ("?" in str(clean_msg) or "？" in str(clean_msg) or
                    any(w in str(clean_msg) for w in ("怎么", "为什么", "如何", "啥", "什么")))
    # Dynamic max_tokens with randomness — wider ranges for natural variation
    # Occasionally give a super-short reply (15% chance, like a lazy real person)
    if random.random() < 0.15:
        dyn_max_tokens = random.randint(10, 30)
    elif is_late_night:
        dyn_max_tokens = random.randint(20, 100)
    elif is_question:
        dyn_max_tokens = random.randint(100, 450)
    elif group_id:
        dyn_max_tokens = random.randint(60, 300)
    else:
        dyn_max_tokens = random.randint(80, 350)  # private chat: wider range

    async def _delayed_ai_request():
        await asyncio.sleep(delay)
        return await _call_deepseek(
            config, messages, max_tokens=dyn_max_tokens,
            temperature=temperature, session=dispatcher.client.session,
        )

    if group_id:
        reply = await _delayed_ai_request()
    else:
        reply = await _await_with_private_typing(
            dispatcher, user_id, _delayed_ai_request())

    # === R18 / inappropriate content interception ===
    # AI uses [R18] marker to flag explicit content - intercept and escalate
    if reply and "[R18]" in reply:
            log.warning("AI rejected user %s in group %s: %s", user_id, group_id, reply[:50])
            # Skip blacklist for bot owner / bot itself
            owner = config.get("bot_owner")
            bot_qq = config.get("bot_qq")
            if user_id == owner or user_id == bot_qq:
                log.info("Skipping R18 escalation for bot owner/self")
            else:
                from .guard import add_warning, get_warning_count, add_blacklist
                gid = group_id if group_id else 0
                add_warning(gid, user_id)
                warn_count = get_warning_count(gid, user_id)
                if warn_count >= 3:
                    add_blacklist(gid, user_id, 48, bot_owner=owner, bot_qq=bot_qq)
                    if group_id:
                        await dispatcher.client.send_group_msg_with_at(group_id,
                            "多次违规，已拉黑48小时。", [user_id])
                    else:
                        await dispatcher.client.send_private_msg(user_id,
                            "多次违规，已拉黑48小时。")
                elif warn_count >= 2:
                    if group_id:
                        await dispatcher.client.send_group_msg_with_at(group_id,
                            "第二次警告，再犯拉黑。", [user_id])
                    else:
                        await dispatcher.client.send_private_msg(user_id,
                            "第二次警告，再犯拉黑。")
                else:
                    if group_id:
                        await dispatcher.client.send_group_msg_with_at(group_id,
                            "警告：请勿发布违规内容。", [user_id])
                    else:
                        await dispatcher.client.send_private_msg(user_id,
                            "警告：请勿发布违规内容。")
            return

    reply = _post_process_reply(reply)

    # === AI chose not to reply: [SKIP] signal ===
    if reply and reply.strip().upper().startswith("[SKIP]"):
        log.debug("AI chose to skip reply for user %s%s", user_id,
                  f" in group {group_id}" if group_id else "")
        _last_reply_ts[context_key] = time.time()
        return False

    if not reply or len(reply.strip()) == 0:
        log.warning("AI returned empty reply for user %s in group %s", user_id, group_id)
        await _notify_ai_unavailable(
            dispatcher, group_id, user_id,
            explicit=(not group_id or reply_intent == "直接回应"),
        )
        return False

    # Delay removed - web search is free and fast now

    # The model, not a local random branch, decides brevity and tone.
    # === AI-driven sticker: parse [STICKER:xxx] tag ===
    wanted_emotion = None
    _sticker_match = re.search(r'\[STICKER:([^\]]+)\]', reply)
    if _sticker_match:
        wanted_emotion = _sticker_match.group(1).strip()
        reply = reply.replace(_sticker_match.group(0), '').strip()

    # Local sticker matching (zero API call)
    sticker_file = None
    if wanted_emotion:
        _sticker_path = os.path.join(STICKER_DIR,
            f"private_{user_id}.json" if not group_id else f"group_{group_id}.json")
        if os.path.exists(_sticker_path):
            try:
                with open(_sticker_path, encoding="utf-8") as _sf:
                    _stickers = json.load(_sf)
                # Exact emotion match first — prefer same-group stickers
                exact_matches = [s for s in _stickers if s.get("emotion", "") == wanted_emotion]
                current_gid = str(group_id) if group_id else f"private_{user_id}"
                same_group = [s for s in exact_matches if s.get("group_id", "") == current_gid]
                matches = same_group if same_group else exact_matches
                # Fallback: match by tags
                if not matches:
                    tag_matches = [s for s in _stickers if wanted_emotion in s.get("tags", [])]
                    same_group_tag = [s for s in tag_matches if s.get("group_id", "") == current_gid]
                    matches = same_group_tag if same_group_tag else tag_matches
                if matches:
                    sticker_file = random.choice(matches)["file"]
                    if not _allow_sticker_send(config, group_id, user_id):
                        sticker_file = None
                    log.info("AI-driven sticker: emotion=%s -> file=%s (from %d matches, same_group=%s)",
                             wanted_emotion, sticker_file[:16], len(matches),
                             bool(same_group or same_group_tag))
                else:
                    log.info("AI wanted sticker emotion=%s but no match found in %d stickers",
                             wanted_emotion, len(_stickers))
                    # Text fallback: replace sticker tag with emoji/kaomoji
                    text_fallbacks = {
                        "开心": "😊", "伤心": "😢", "生气": "😠", "无语": "😅",
                        "惊讶": "😮", "害羞": "😳", "尴尬": "😅", "得意": "😏",
                        "困惑": "🤔", "拒绝": "🙅", "赞同": "👍", "嘲讽": "🙄",
                        "感谢": "🙏", "安慰": "🤗", "庆祝": "🎉", "卖萌": "🥺",
                        "敷衍": "😐", "打招呼": "👋", "告别": "👋", "晚安": "🌙",
                        "点赞": "👍",
                    }
                    fallback = text_fallbacks.get(wanted_emotion, "")
                    if fallback and not reply.rstrip().endswith(fallback):
                        reply = (reply + fallback).strip()
            except Exception as e:
                log.error("Sticker matching error: %s", e)

    # === Anti-echo guard: skip if reply is too similar to recent replies ===
    if user_id and _is_repetitive(user_id, reply):
        log.info("Anti-echo: skipping repetitive reply to user %s: %s", user_id, reply[:60])
        return None

    if group_id:
        try:
            # Build member map for @ parsing
            member_map = {}
            if hasattr(dispatcher, "_group_member_cache"):
                cache = dispatcher._group_member_cache.get(group_id, {})
                for nick, qq in cache.items():
                    if nick and qq:
                        member_map[nick] = qq

            clean_reply, tagged_actions = _parse_reply_tags(reply, member_map)
            at_qqs = [int(a["qq"]) for a in tagged_actions
                      if a.get("type") == "at" and str(a.get("qq", "")).isdigit()]
            wants_reply = any(a.get("type") == "reply" for a in tagged_actions)
            poke_targets = [a.get("target") for a in tagged_actions if a.get("type") == "poke"]
            # Backward-compatible natural @nickname and quoted-text parsing.
            clean_reply, legacy_at, quote_text = _parse_reply_actions(clean_reply, member_map)
            at_qqs.extend(legacy_at)
            at_qqs = list(dict.fromkeys(at_qqs))[:2]
            if wants_reply and message_id:
                quote_text = quote_text or "reply"
            if not clean_reply:
                clean_reply = reply

            # === AI Voice: occasionally send short replies as voice instead of text ===
            voice_used = False
            if not at_qqs and not quote_text and not sticker_file and len(clean_reply) <= 15 and any(a.get("type") == "voice" for a in tagged_actions):
                voice_used = await _maybe_send_as_voice(dispatcher, group_id, clean_reply, is_late_night)

            if not voice_used:
                # === Message splitting: mimic human sequential sending ===
                if _should_split_reply(clean_reply, is_private=False):
                    segments = _split_reply_segments(clean_reply)
                    for i, seg in enumerate(segments):
                        seg = _random_trim_punctuation(seg)
                        if not seg:
                            continue
                        _segs = []
                        # @mention only on first segment
                        if i == 0 and at_qqs:
                            _at_segs = [{"type": "at", "data": {"qq": str(qq)}} for qq in at_qqs[:2]]
                            _at_segs.append({"type": "text", "data": {"text": seg}})
                            _segs = _at_segs
                        else:
                            _segs.append({"type": "text", "data": {"text": seg}})
                        # Sticker on last segment
                        if i == len(segments) - 1 and sticker_file:
                            _segs.append({"type": "image", "data": {"file": sticker_file}})
                        # Quote only on first segment
                        if quote_text and message_id and i == 0:
                            await dispatcher.client.send_group_msg_reply(group_id, _segs, message_id)
                        else:
                            await dispatcher.client.send_group_msg(group_id, _segs)
                        # Natural gap between segments
                        if i < len(segments) - 1:
                            await asyncio.sleep(random.uniform(0.5, 2.0))
                    log.debug("Split reply into %d segments for group %s", len(segments), group_id)
                else:
                    # No split — single message
                    _segs = []
                    if clean_reply:
                        _segs.append({"type": "text", "data": {"text": clean_reply}})
                    if sticker_file:
                        _segs.append({"type": "image", "data": {"file": sticker_file}})
                    if not _segs:
                        _segs = [{"type": "text", "data": {"text": reply}}]

                    if quote_text and message_id:
                        if at_qqs:
                            _at_segs = [{"type": "at", "data": {"qq": str(qq)}} for qq in at_qqs[:2]]
                            _at_segs.extend(_segs)
                            await dispatcher.client.send_group_msg_reply(group_id, _at_segs, message_id)
                        else:
                            await dispatcher.client.send_group_msg_reply(group_id, _segs, message_id)
                    elif at_qqs:
                        _at_segs = [{"type": "at", "data": {"qq": str(qq)}} for qq in at_qqs[:2]]
                        _at_segs.extend(_segs)
                        await dispatcher.client.send_group_msg(group_id, _at_segs)
                    else:
                        await dispatcher.client.send_group_msg(group_id, _segs)
            for target in poke_targets[:1]:
                if target:
                    await dispatcher.client.group_poke(group_id, target)
        except Exception as e:
            log.error("Reply send error: %s", e, exc_info=True)
            await dispatcher.client.send_group_msg(group_id, reply)
    else:
        clean_reply, _, _ = _parse_reply_actions(reply, {})
        if not clean_reply:
            clean_reply = reply

        # Private chat splitting (lower probability, more chill)
        try:
            if _should_split_reply(clean_reply, is_private=True):
                segments = _split_reply_segments(clean_reply)
                for i, seg in enumerate(segments):
                    seg = _random_trim_punctuation(seg)
                    if not seg:
                        continue
                    _segs = [{"type": "text", "data": {"text": seg}}]
                    if i == len(segments) - 1 and sticker_file:
                        _segs.append({"type": "image", "data": {"file": sticker_file}})
                    await dispatcher.client.send_private_msg(user_id, _segs)
                    if i < len(segments) - 1:
                        await asyncio.sleep(random.uniform(0.5, 2.0))
                log.debug("Split private reply into %d segments for user %s", len(segments), user_id)
            else:
                _segs = []
                if clean_reply:
                    _segs.append({"type": "text", "data": {"text": clean_reply}})
                if sticker_file:
                    _segs.append({"type": "image", "data": {"file": sticker_file}})
                await dispatcher.client.send_private_msg(user_id, _segs if _segs else clean_reply)
        except Exception as e:
            log.error("Private reply send error (sticker may be stale): %s", e)
            # Fallback: text-only retry
            await dispatcher.client.send_private_msg(user_id, clean_reply or reply)

    # Track last reply timestamp for multi-layer delay
    _last_reply_ts[context_key] = time.time()
    # Track reply content for anti-echo
    if user_id:
        _record_reply(user_id, clean_reply if clean_reply else reply)

    # Learn from conversation & save memory

    from .memory import extract_user_info

    user_msg_text = original_clean_msg or clean_msg or raw_message
    learned = extract_user_info(user_msg_text)
    now = time.time()

    if group_id:
        # === Group chat memory ===
        user_mem = _load_user_memory(group_id, user_id)
        for info in learned:
            user_mem.append({"role": "system", "content": info, "ts": now})
        user_mem.append({"role": "user", "content": "{}: {}".format(sender_name, user_msg_text), "ts": now})
        user_mem.append({"role": "assistant", "content": reply, "ts": now})
        _save_user_memory(group_id, user_id, user_mem, config)

        memory.append({"role": "user", "content": "{}: {}".format(sender_name, user_msg_text)})
        memory.append({"role": "assistant", "content": reply})
        _save_memory(group_id, memory, config, dispatcher.client.session)

        # Append bot reply to group buffer so _build_chat_context includes our own messages
        try:
            bot_qq = config.get("bot_qq", 0)
            bot_card = "小汐"
            clean_reply_for_buffer = reply.replace(chr(10), chr(32)).replace(chr(13), chr(32))[:100]
            dispatcher.append_to_buffer(group_id, bot_qq, bot_card + ": " + clean_reply_for_buffer, bot_card)
        except Exception as e:
            log.debug("Failed to append bot reply to buffer: %s", e)
    else:
        # === Private chat memory (deeper: 30 entries + API long-term compression) ===
        user_mem = _load_user_memory(0, user_id)
        for info in learned:
            user_mem.append({"role": "system", "content": info, "ts": now})
        user_mem.append({"role": "user", "content": "{}: {}".format(sender_name, user_msg_text), "ts": now})
        user_mem.append({"role": "assistant", "content": reply, "ts": now})
        for e in user_mem:
            if "ts" not in e:
                e["ts"] = now
        private_max = 30
        if len(user_mem) > private_max:
            overflow = user_mem[:len(user_mem) - private_max]
            user_mem = user_mem[-private_max:]
            if config and dispatcher.client.session and len(overflow) >= 4:
                _schedule_long_memory(
                    _compress_private_to_long(user_id, overflow, config, dispatcher.client.session)
                )
        from .memory import sanitize_for_memory
        for entry in user_mem:
            if "content" in entry:
                entry["content"] = sanitize_for_memory(entry.get("content", ""))
        atomic_write_json(_user_memory_file(0, user_id), user_mem)

    return True


def _allow_sticker_send(config, group_id, user_id):
    """Resource/spam boundary only; AI still decides whether a sticker fits."""
    cfg = config.get("sticker_mode", {})
    now = time.time()
    key = "g:{}".format(group_id) if group_id else "u:{}".format(user_id)
    cooldown = int(cfg.get("group_cooldown_seconds", 180) if group_id
                   else cfg.get("private_cooldown_seconds", 90))
    if now - _STICKER_LAST_SENT.get(key, 0) < cooldown:
        return False
    day_key = time.strftime("%Y%m%d") + ":" + key
    limit = int(cfg.get("daily_send_limit", 12))
    if _STICKER_DAILY_COUNT.get(day_key, 0) >= limit:
        return False
    _STICKER_LAST_SENT[key] = now
    _STICKER_DAILY_COUNT[day_key] = _STICKER_DAILY_COUNT.get(day_key, 0) + 1
    if len(_STICKER_DAILY_COUNT) > 500:
        today = time.strftime("%Y%m%d") + ":"
        for item in list(_STICKER_DAILY_COUNT):
            if not item.startswith(today):
                _STICKER_DAILY_COUNT.pop(item, None)
    return True


def _should_consider_napcat_tool(text):
    value = str(text or "").lower()
    keywords = (
        "群信息", "群资料", "群人数", "成员", "谁是", "群主", "管理员",
        "聊天记录", "历史消息", "刚才说", "群文件", "文件链接", "群公告",
        "群荣誉", "龙王", "禁言列表", "qq资料", "qq信息",
    )
    return any(keyword in value for keyword in keywords)


async def _maybe_call_napcat_tool(dispatcher, group_id, user_id, text, chat_context):
    """Ask the model for at most one whitelisted read-only tool call."""
    prompt = (
        "你负责选择是否调用QQ/NapCat只读工具。只输出一行JSON或NONE。\n"
        "可用工具：\n"
        "get_group_info 参数 group_id\n"
        "get_member_info 参数 group_id,user_id\n"
        "get_recent_messages 参数 group_id,count(1-20)\n"
        "get_group_files 参数 group_id,keyword\n"
        "get_group_notice 参数 group_id\n"
        "get_group_honor 参数 group_id,honor_type\n"
        "get_shut_list 参数 group_id\n"
        "get_friend_info 参数 user_id\n"
        "当前群号和用户号由系统提供，不得编造。\n"
        "示例：{\"tool\":\"get_group_info\",\"arguments\":{}}\n"
        "如果无需工具只输出NONE。"
    )
    user_prompt = "当前群={} 当前用户={} 消息={}\n最近上下文={}".format(
        group_id, user_id, str(text)[:160], str(chat_context or "")[-500:])
    decision = await _call_deepseek(
        dispatcher.config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_prompt}],
        max_tokens=80, temperature=0.1, session=dispatcher.client.session)
    if not decision or decision.strip().upper().startswith("NONE"):
        return ""
    try:
        match = re.search(r'\{.*\}', decision, re.S)
        payload = json.loads(match.group(0) if match else decision)
        name = payload.get("tool", "")
        args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        if group_id:
            args["group_id"] = group_id
        if name == "get_member_info" and not args.get("user_id"):
            args["user_id"] = user_id
        from ai_tools import execute_tool, format_tool_result
        result = await execute_tool(dispatcher, name, args)
        return format_tool_result(result)
    except Exception as exc:
        log.debug("NapCat tool decision ignored: %s", exc)
        return ""

# ========== RELEVANCE JUDGE ==========

async def judge_relevance(dispatcher, group_id, user_id, raw_message, sender_name,
                           chat_context, is_followup=False, web_context=""):
    """Quick AI check: is this message worth responding to?"""
    if not raw_message or len(raw_message.strip()) < 2:
        return False

    import re as _re
    text_only = _re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
    if len(text_only) < 2:
        return False

    config = dispatcher.config

    if is_followup:
        follow_hint = (
            "【重要】你刚才正在和这个人聊天，对方这句话大概率是在回复你刚刚说的话。\n"
            "除非这句话明显是在跟别人说（比如@了别人、提到了别人的名字），否则就应该回复。\n"
        )
    else:
        follow_hint = ""

    web_info = ""
    if web_context:
        web_info = f"【联网搜索结果】\n{web_context[:400]}\n\n"

    prompt = (
        f"群聊上下文（最近几条消息，包含你说的话）:\n{chat_context[:500]}\n\n"
        f"{web_info}"
        f"{follow_hint}"
        f"{sender_name} 刚说: {text_only[:120]}\n\n"
        "【判断流程 — 严格按顺序，命中即停】\n"
        "1. 群聊上下文或当前消息中，有人在讨论/提及/评价你（小汐/汐汐）吗？\n"
        "   例：问你在不在、说你坏话、讨论你说过的话、评价你这个人 → 回复 是\n"
        "2. 对方在跟你说话、回复你刚才的话、接你话题？ → 回复 是\n"
        "3. 搜索结果显示你了解这话题，且能自然接话不突兀？ → 回复 是\n"
        "4. 以上都不满足 → 回复 否\n"
        "只回复 是 或 否。不要解释。"
    )

    messages = [
        {"role": "system", "content": "你是小汐，一个普通群友。快速判断一条消息是否值得你回复。\n规则：\n1. 有人在讨论你（小汐）→ 必须回复 是\n2. 对方在跟你说话 → 回复 是\n3. 你了解话题且能自然接话 → 回复 是\n4. 否则 否\n只输出 是 或 否。"},
        {"role": "user", "content": prompt}
    ]

    reply = await _call_deepseek(config, messages, max_tokens=5, temperature=0.1,
                                  session=dispatcher.client.session)
    if reply:
        result = reply.strip().startswith("是")
        if not result:
            log.debug("AI judged irrelevant: %s", text_only[:50])
        return result
    return is_followup

# ========== INTERJECTION (自主插话) ==========

async def generate_interjection(dispatcher, group_id, context_lines):
    """AI decides whether and how to interject based on group context."""
    config = dispatcher.config
    memory = _load_memory(group_id) if group_id else []
    
    # Build recent memory
    mem_str = ""
    if memory:
        recent_mem = memory[-10:]
        mem_lines = []
        for m in recent_mem:
            mem_lines.append(m["content"][:60].replace("\n", " "))
        if mem_lines:
            mem_str = "最近聊过: " + "; ".join(mem_lines)

    context_str = "\n".join(context_lines)
    system = (
        PERSONALITY + "\n\n" + SAFETY_RULES + f"\n\n现在是北京时间 {datetime.now(timezone(timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M')}，星期{'一二三四五六日'[datetime.now(timezone(timedelta(hours=8))).weekday()]}。\n\n"
        "【插话任务 - 严格限制】\n"
        "下面是群里的聊天记录。请严格遵守以下规则判断是否插话：\n"
        "1. 话题是否直接与你（小汐）相关？比如在讨论你、评价你？\n"
        "2. 话题是否在你的核心兴趣范围内（ACG/动漫/游戏/追剧/音乐）？\n"
        "3. 群友是否明确在寻求帮助或意见？\n\n"
        "只有满足以上至少一条，你才可以插话。否则必须回复 不说。\n"
        "记住：你是偶尔冒泡的群友，不是24小时客服。大多数消息你应该回复 不说。\n"
        "如果插话，用1-2句话简短自然地参与，不要多说。"
    )
    if mem_str:
        system += "\n\n" + mem_str

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "群聊记录:\n" + context_str + "\n\n请判断是否插话。如果需要插话请直接回复聊天内容，否则回复 不说。"}
    ]

    reply = await _call_deepseek(config, messages, max_tokens=80, temperature=0.7,
                                  session=dispatcher.client.session)
    if reply:
        reply = _post_process_reply(reply)
        if reply.strip().startswith("不说"):
            return None
        return reply
    return None



# ========== REPLY PARSING ==========

def _parse_reply_actions(reply, member_map):
    """Parse AI reply for @mentions and quote markers.
    member_map: dict of {nickname: qq_number}
    Returns: (clean_reply, at_qqs, quote_text)
    """
    import re as _re
    at_qqs = []
    quote_text = None
    
    # Extract 「quoted text」
    quote_match = _re.search(r'「([^」]+)」', reply)
    if quote_match:
        quote_text = quote_match.group(1)
        reply = reply.replace(quote_match.group(0), '')
    
    # Extract @nickname patterns
    at_pattern = _re.compile(r'@(\S{1,16})')
    for m in at_pattern.finditer(reply):
        nick = m.group(1)
        # Remove punctuation from end of nick
        nick = _re.sub(r'[^一-鿿\w]+$', '', nick)
        if nick and nick in member_map:
            at_qqs.append(member_map[nick])
            reply = reply.replace(m.group(0), '', 1)
    
    # Clean up extra whitespace
    reply = _re.sub(r'\s+', ' ', reply).strip()
    
    return reply, at_qqs, quote_text

# ========== IMAGE DESCRIPTION (识图) ==========

async def describe_image(dispatcher, group_id, file_id, sub_type, summary=""):
    """Describe image content. Vision API (Qwen) first, QQ summary as fallback."""
    config = dispatcher.config
    import html as _html

    # Decode QQ summary for potential fallback use
    qq_summary = ""
    if summary:
        qq_summary = _html.unescape(summary).strip()

    # Try vision API first
    image_url = None
    try:
        result = await dispatcher.client.call("get_image", {"file": file_id})
        if result.get("status") == "ok":
            data = result.get("data", {})
            image_url = data.get("url") or data.get("file")
    except Exception as e:
        log.error("get_image failed: %s", e)

    if image_url:
        log.info("Vision API: describing %s", file_id[:16])
        desc = await _call_vision_api(config, image_url, session=dispatcher.client.session)
        if desc:
            log.info("Vision result: %s -> %s", file_id[:16], desc[:50])
            return desc

    # Fallback: use QQ summary if vision API failed or image URL unavailable
    if qq_summary:
        log.info("Image via summary (fallback): %s -> %s", file_id[:16], qq_summary[:50])
        return qq_summary

    # Ultimate fallback
    if sub_type and str(sub_type) != "0":
        return "[表情/贴纸]"
    return "[图片]"

import re as _re_sticker

# ---- Collect sticker - now with vision analysis ----
async def collect_sticker_async(dispatcher, group_id, file_id, sub_type, summary="",
                                    is_private=False):
    """Collect sticker with AI vision analysis. Called from dispatcher."""
    sticker_cfg = dispatcher.config.get("sticker_mode", {})
    if not sticker_cfg.get("collect", True):
        return
    prefix = "private" if is_private else "group"
    path = os.path.join(STICKER_DIR, f"{prefix}_{group_id}.json")
    stickers = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                stickers = json.load(f)
        except Exception:
            pass

    # Avoid duplicates
    if any(s.get("file") == file_id for s in stickers):
        return

    # Group chat sampling: only collect ~30% to avoid overload (private chat collects all)
    if not is_private and random.random() > 0.3:
        return

    desc_text = ""
    emotion = ""
    tags = []
    usage_scene = ""
    if summary:
        import html as _html_st
        desc_text = _html_st.unescape(summary)[:50]

    # Reuse dispatcher image cache if available (avoid duplicate vision API call)
    cached_entry = None
    img_cache = getattr(dispatcher, "_image_desc_cache", None)
    if img_cache and file_id in img_cache:
        entry = img_cache[file_id]
        cached_entry = entry if isinstance(entry, dict) else {"desc": str(entry)}

    # Always try vision API for new stickers (free quota, one-time cost)
    image_url = None
    if not desc_text:
        try:
            result = await dispatcher.client.call("get_image", {"file": file_id})
            if result.get("status") == "ok":
                data = result.get("data", {})
                image_url = data.get("url") or data.get("file")
        except Exception:
            pass

    # Call vision API for detailed analysis (or use cached description)
    if cached_entry and not desc_text:
        desc_text = cached_entry.get("desc", "")[:50]
        emotion = cached_entry.get("emotion", "")
        tags = cached_entry.get("tags", [])
        usage_scene = cached_entry.get("usage", "")
    elif image_url:
        result = await _analyze_sticker_vision(dispatcher.config, image_url,
                                               session=dispatcher.client.session)
        if result:
            # Parse structured response: description|emotion|tags|usage
            parts = result.split("|")
            if len(parts) >= 1:
                desc_text = parts[0].strip()
            if len(parts) >= 2:
                emotion = parts[1].strip()
            if len(parts) >= 3:
                tags = [t.strip() for t in parts[2].split(",") if t.strip()]
            if len(parts) >= 4:
                usage_scene = parts[3].strip()
    stickers.append({
        "file": file_id,
        "sub_type": sub_type,
        "desc": desc_text,
        "emotion": emotion,
        "tags": tags,
        "usage": usage_scene,
        "group_id": f"private_{group_id}" if is_private else str(group_id),
        "ts": time.time()
    })

    # Keep max 50 stickers
    max_stickers = 50
    if len(stickers) > max_stickers:
        stickers = stickers[-max_stickers:]

    atomic_write_json(path, stickers)
    log.info("Sticker collected + analyzed: %s -> %s [%s]", file_id[:16], desc_text[:40], emotion or "?")


async def _analyze_sticker_vision(config, image_url, session=None):
    """Use vision API to analyze sticker: description, tags, category, usage."""
    runtime = config.get("runtime", {})
    async with _get_semaphore("vision", runtime.get("vision_concurrency", 1)):
        return await _analyze_sticker_vision_inner(config, image_url, session)


async def _analyze_sticker_vision_inner(config, image_url, session=None):
    """Use vision API to analyze sticker: description, tags, category, usage."""
    vision_cfg = config.get("vision_api", {})
    if not vision_cfg:
        return None
    api_key = _get_vision_api_key(config)
    if not api_key:
        return None
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json"
    }
    prompt = (
        "请描述这张表情包。用以下格式回复（严格4段，用|分隔）：\n"
        "简短描述(15字内)|情绪标签|关键词1,关键词2|适用场景(10字内)\n"
        "情绪标签必须从以下选一个：开心 伤心 生气 无语 惊讶 害羞 尴尬 得意 困惑 拒绝 赞同 嘲讽 感谢 安慰 庆祝 卖萌 敷衍 打招呼 告别 晚安 点赞\n"
        "示例：猫翻白眼|无语|翻白眼,猫|对无语的事表示同感"
    )
    payload = {
        "model": vision_cfg.get("model", "qwen-vl-plus"),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        }],
        "max_tokens": 100,
        "temperature": 0.3,
    }
    url = vision_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1") + "/chat/completions"

    async def _do_post(sess):
        async with sess.post(url, headers=headers, json=payload,
                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
            else:
                body = await resp.text()
                log.warning("Vision API sticker returned %d: %s", resp.status, body[:150])

    try:
        if session:
            return await _do_post(session)
        async with aiohttp.ClientSession() as s:
            return await _do_post(s)
    except Exception as e:
        log.error("Sticker vision analysis failed: %s", e)
    return None


# ---- Best sticker picker ----
async def _pick_best_sticker(dispatcher, group_id, stickers):
    """Pick the most contextually relevant sticker using AI."""
    if len(stickers) <= 1:
        return stickers[0] if stickers else None
    buffer = list(dispatcher._group_msg_buffer.get(group_id, []))
    if not buffer:
        return None
    recent = buffer[-5:]
    context_lines = []
    for uid, txt, ts, card in recent:
        clean = txt[:80].replace("\n", " ")
        context_lines.append(f"{card}: {clean}")
    context_str = "\n".join(context_lines)

    # Build sticker list with rich descriptions
    sticker_descs = []
    for i, s in enumerate(stickers[:15]):
        desc = s.get("description", "") or s.get("summary", "") or "表情"
        tags = s.get("tags", [])
        usage = s.get("usage", "")
        extra = ""
        if tags:
            extra += " [" + ",".join(tags[:3]) + "]"
        if usage:
            extra += " (" + usage + ")"
        sticker_descs.append(f"[{i}] {desc}{extra}")
    sticker_list = "\n".join(sticker_descs)

    config = dispatcher.config
    prompt = (
        f"最近聊天内容:\n{context_str}\n\n"
        f"可选表情包:\n{sticker_list}\n\n"
        "根据聊天语境，选择一个最合适的表情包回复。只回复数字编号(0-9)，不要解释。如果不适合发任何表情包，回复-1。"
    )
    try:
        choice_text = await _call_deepseek(
            config,
            [{"role": "user", "content": prompt}],
            max_tokens=5, temperature=0.3,
            session=dispatcher.client.session
        )
        if choice_text:
            match = _re_sticker.search(r"-?\d+", choice_text.strip())
            if match:
                idx = int(match.group())
                if 0 <= idx < len(stickers):
                    log.info("Smart sticker pick: #%d (%s) for group %s",
                            idx, stickers[idx].get("description", "?"), group_id)
                    return stickers[idx]
                elif idx == -1:
                    return None  # AI decided no sticker fits
    except Exception as e:
        log.error("Smart sticker pick failed: %s", e)
    return None

# ---- Send sticker ----
async def _maybe_send_sticker(dispatcher, group_id, is_private=False):
    """Send a contextual sticker in private or group chat."""
    sticker_cfg = dispatcher.config.get("sticker_mode", {})
    if not sticker_cfg.get("enabled", True):
        return
    prob = sticker_cfg.get("send_probability", 0.15)
    if random.random() > prob:
        return

    # Private chat uses a per-user sticker file, groups use per-group
    path = os.path.join(STICKER_DIR,
                        f"private_{group_id}.json" if is_private else f"group_{group_id}.json")
    if not os.path.exists(path):
        return

    try:
        with open(path, encoding="utf-8") as f:
            stickers = json.load(f)
    except Exception:
        return

    if not stickers:
        return

    if sticker_cfg.get("smart_pick", False):
        chosen = await _pick_best_sticker(dispatcher, group_id, stickers)
        if not chosen:
            return
    else:
        chosen = random.choice(stickers[-15:])
    msg = [{
        "type": "image",
        "data": {
            "file": chosen["file"],
            "sub_type": chosen.get("sub_type", "0")
        }
    }]
    try:
        if is_private:
            await dispatcher.client.send_private_msg(group_id, msg)
        else:
            await dispatcher.client.send_group_msg(group_id, msg)
        log.info("Sent %s sticker to %s: %s",
                 "private" if is_private else "group", group_id,
                 chosen.get("description", "?"))
    except Exception as e:
        log.error("Failed to send sticker: %s", e)

# ---- Sticker summaries for /list ----
def get_sticker_summaries(group_id):
    """Get sticker info for /list command."""
    path = os.path.join(STICKER_DIR, f"group_{group_id}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            stickers = json.load(f)
    except Exception:
        return []
    summaries = []
    for s in stickers:
        desc = s.get("desc") or s.get("description") or s.get("summary", "") or "无描述"
        emotion = s.get("emotion", "")
        tags = s.get("tags", [])
        usage = s.get("usage", "")
        line = desc
        if emotion:
            line += " [" + emotion + "]"
        if tags:
            line += " [" + ",".join(tags[:3]) + "]"
        if usage:
            line += " - " + usage
        summaries.append({"description": desc, "emotion": emotion, "tags": tags, "usage": usage,
                          "display": line})
    return summaries


def _build_sticker_inventory(group_id=None, user_id=None, is_private=False):
    """Build sticker inventory summary by emotion for system prompt.
    Tells AI what stickers are available so it can decide whether to use [STICKER:xxx]."""
    gid = user_id if is_private else group_id
    if not gid:
        return ""
    prefix = "private" if is_private else "group"
    path = os.path.join(STICKER_DIR, f"{prefix}_{gid}.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            stickers = json.load(f)
    except Exception:
        return ""
    if not stickers:
        return ""
    # Group by emotion, collect up to 2 descriptions per emotion
    by_emotion = {}
    for s in stickers:
        em = s.get("emotion", "")
        if not em:
            tags = s.get("tags", [])
            em = tags[0] if tags else "其他"
        if em not in by_emotion:
            by_emotion[em] = []
        desc = s.get("desc") or s.get("description", "") or ""
        if desc and desc not in by_emotion[em]:
            by_emotion[em].append(desc[:10])
    total = len(stickers)
    lines = []
    for em in sorted(by_emotion):
        samples = by_emotion[em][:2]
        count = len(by_emotion[em])
        lines.append(f"{em}({count}): " + "、".join(samples))
    summary = "\n".join(lines)
    return (f"【你收藏的表情包（共{total}个）】\n{summary}\n"
            "回复时如果觉得发个表情包能更好表达情绪，在末尾加 [STICKER:情绪标签]。")

# ========== WEB SEARCH ==========

async def search_web(dispatcher, query):
    """Search web using Bing (free, works in mainland China)."""
    config = dispatcher.config
    ws_cfg = config.get("web_search", {})
    if not ws_cfg.get("enabled", True):
        return ""
    
    import re as _re_ws
    query = _re_ws.sub(r"\s+", " ", (query or "")).strip()
    if len(query) < 4:
        return ""
    cache_key = query.lower()[:120]
    cache = getattr(dispatcher, "_web_search_cache", None)
    now = time.time()
    if cache is not None:
        cached = cache.get(cache_key)
        if cached:
            age = now - cached.get("ts", 0)
            hit_value = cached.get("value", "")
            # Successful results: full TTL. Empty/failed results: short TTL (120s).
            effective_ttl = _SEARCH_CACHE_TTL if hit_value else 120
            if age < effective_ttl:
                return hit_value
    
    try:
        async with dispatcher._search_sem:
            encoded = urllib.parse.quote(query)
            url = f"https://www.bing.com/search?q={encoded}&setlang=zh-cn"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            timeout = aiohttp.ClientTimeout(total=6)
            value = ""

            if dispatcher.client.session:
                session = dispatcher.client.session
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        value = _parse_bing_results(html, query)
            else:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, headers=headers, timeout=timeout) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                            value = _parse_bing_results(html, query)
            if cache is not None:
                cache[cache_key] = {"ts": now, "value": value}
                if len(cache) > 100:
                    # Lazy cleanup: remove entries older than 30 min (bulk of stale cache)
                    cutoff = now - 1800
                    stale = [k for k, v in cache.items() if now - v.get("ts", 0) > 1800]
                    for key in stale[:50]:
                        cache.pop(key, None)
                    # If still over limit, do a full sort once
                    if len(cache) > 100:
                        oldest = sorted(cache.items(), key=lambda item: item[1].get("ts", 0))[:20]
                        for key, _ in oldest:
                            cache.pop(key, None)
                        cache.pop(key, None)
            return value
    except Exception as e:
        log.error("Web search error: %s", e)
    
    return ""

def _parse_bing_results(html, query):
    """Parse Bing HTML search results with multi-layer fallback."""
    import re as _re_b

    results = []

    # Layer 1: standard b_algo blocks
    blocks = _re_b.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)

    for block in blocks[:3]:
        title_m = _re_b.search(r'<h2[^>]*><a[^>]*>(.*?)</a>', block, re.DOTALL)
        snippet_m = _re_b.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)

        if title_m:
            title = _re_b.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            title = title.replace("&ensp;", " ").replace("&emsp;", " ")

            snippet = ""
            if snippet_m:
                snippet = _re_b.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()
                snippet = snippet.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                snippet = snippet.replace("&ensp;", " ").replace("&emsp;", " ")
                # Remove date prefixes like "2025年12月15日"
                snippet = _re_b.sub(r'^\d{4}年\d{1,2}月\d{1,2}日\s*', '', snippet)

            line = title[:100]
            if snippet:
                line += "\n  " + snippet[:150]
            results.append(line)

    # Layer 2: fallback to b_caption / generic result snippets
    if not results:
        alt_blocks = _re_b.findall(r'<li class="b_caption"[^>]*>(.*?)</li>', html, re.DOTALL)
        if not alt_blocks:
            alt_blocks = _re_b.findall(r'<div class="b_caption"[^>]*>(.*?)</div>', html, re.DOTALL)
        for block in alt_blocks[:3]:
            text = _re_b.sub(r'<[^>]+>', ' ', block)
            text = _re_b.sub(r'\s+', ' ', text).strip()
            if len(text) > 20:
                results.append(text[:250])

    # Layer 3: extract page title as confirmation search worked
    if not results:
        title_m = _re_b.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE)
        if title_m:
            title = title_m.group(1).strip()
            if "No results" not in title and "没有结果" not in title:
                results.append("搜索已完成，但未能解析详情")

    if not results:
        return ""

    return "\n".join(results[:3])

# ========== POST-PROCESSING ==========

def _post_process_reply(reply):
    """Clean up AI reply."""
    import re as _re
    if not reply:
        return ""
    # Strip ALL bracket action descriptions like (笑)(挠头)(托腮) etc.
    reply = _re.sub(r'[(〈][^\)〉]{1,8}[\)〉]', '', reply)
    # Also strip （xxx） full-width brackets
    reply = _re.sub(r'（[^）]{1,8}）', '', reply)
    # Remove code blocks
    reply = reply.replace("```", "")
    banned_prefixes = (
        "作为AI", "作为一个AI", "作为人工智能", "根据参考信息", "根据搜索结果",
        "我查了一下", "从资料来看", "总结一下", "简单来说，",
    )
    for prefix in banned_prefixes:
        if reply.startswith(prefix):
            reply = reply[len(prefix):].lstrip("：: ，,")
    reply = _re.sub(r"^(首先|其次|最后)[，,：:]\s*", "", reply)
    # Remove excessive newlines
    while "\n\n\n" in reply:
        reply = reply.replace("\n\n\n", "\n\n")
    # Strip and re-space
    reply = _re.sub(r'  +', ' ', reply).strip()
    # Limit length
    if len(reply) > 500:
        reply = reply[:500] + "..."
    return reply


# ========== REPLY TAG PARSER (STICKER/REPLY/POKE/AT) ==========

def _parse_reply_tags(reply, member_map):
    """Parse feature tags from AI reply text.

    member_map: dict of {nickname: qq_number} for @ resolution.

    Returns:
        clean_reply (str): reply with tags stripped
        actions (list): list of action dicts to execute
    """
    import re as _re_tag
    actions = []

    # 1. [STICKER:emotion] — already handled elsewhere
    reply = _re_tag.sub(r'\[STICKER:[^\]]+\]', '', reply)

    # 2. [POKE:nickname]
    _poke_match = _re_tag.search(r'\[POKE:([^\]]+)\]', reply)
    if _poke_match:
        nick = _poke_match.group(1).strip()
        qq = member_map.get(nick, 0)
        if qq:
            actions.append({"type": "poke", "target": qq})
        reply = reply.replace(_poke_match.group(0), '').strip()

    # 3. [AT:nickname] — resolve nickname to QQ
    while True:
        _at_match = _re_tag.search(r'\[AT:([^\]]+)\]', reply)
        if not _at_match:
            break
        nick = _at_match.group(1).strip()
        qq = member_map.get(nick, 0)
        actions.append({"type": "at", "qq": str(qq) if qq else nick})
        reply = reply.replace(_at_match.group(0), '@' + nick, 1)

    # 4. [REPLY] — flag to reply to the original message
    if '[VOICE]' in reply:
        actions.append({"type": "voice"})
        reply = reply.replace('[VOICE]', '').strip()

    if '[REPLY]' in reply:
        actions.append({"type": "reply"})
        reply = reply.replace('[REPLY]', '').strip()

    # 5. Clean up whitespace
    reply = _re_tag.sub(r'\s+', ' ', reply).strip()

    return reply, actions


async def _maybe_send_as_voice(dispatcher, group_id, reply, is_late_night):
    """Try to send a short reply as AI voice instead of text.

    Only for short replies (≤ 15 chars), with probability varying by time.
    Returns True if voice was sent, False if should fall back to text.
    """
    if not group_id:
        return False  # Voice only supported for group chat currently
    if not reply or len(reply) > 15:
        return False

    # Probability: 8% normally, 18% late night (sleepy, don't want to type)
    voice_chance = 0.18 if is_late_night else 0.08
    if random.random() > voice_chance:
        return False

    # Default character ID — young female voice
    # Can be overridden via config: voice_character
    character = dispatcher.config.get("voice_character", "2")

    try:
        await dispatcher.client.send_group_ai_record(group_id, character, reply)
        log.info("Voice sent: group=%s char=%s text=%s", group_id, character, reply[:20])
        return True
    except Exception as e:
        log.debug("Voice send failed (will fall back to text): %s", e)
        return False


def _should_split_reply(text, is_private=False):
    """Decide whether to split reply into multiple messages for human-like pacing.

    Splits if text >= 4 chars (private) or >= 8 chars (group).
    Private chat: 75% chance. Group chat: 60%.
    """
    # Message shape is selected by the model through explicit tags; never
    # split ordinary replies using a local random probability.
    return False
    if not text:
        return False
    if is_private:
        if len(text) < 4:
            return False
        split_chance = 0.75
    else:
        if len(text) < 8:
            return False
        split_chance = 0.60
    return random.random() < split_chance


def _split_reply_segments(text):
    """Split reply text into natural segments mimicking how a person sends messages.

    Splits at sentence boundaries (。！？) and newlines first. Longer segments (>18 chars)
    are further split at commas. For unpunctuated text > 20 chars, force-splits at commas
    or midpoints to ensure multi-message delivery.
    """
    import re as _re
    if not text:
        return [""]

    # Step 1: split by sentence-ending punctuation and newlines
    parts = _re.split(r'(?<=[。！？\n])', text)

    # Step 2: if no sentence breaks found and text is long, force-split
    if len(parts) == 1 and len(text) > 20:
        # Try splitting at commas / semicolons first
        comma_parts = _re.split(r'(?<=[，,、；;])', text)
        if len(comma_parts) > 1:
            parts = comma_parts

    # Step 3: refine long segments
    result = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > 18:
            # Further split by commas / semicolons
            sub = _re.split(r'(?<=[，,、；;])', part)
            for s in sub:
                s = s.strip()
                if s:
                    # If still very long (>30 chars) and no punctuation, chop by length
                    if len(s) > 30 and not _re.search(r'[，,、；;。！？]', s):
                        # Split into ~15 char chunks at character boundaries
                        chunk_size = random.randint(12, 18)
                        for j in range(0, len(s), chunk_size):
                            chunk = s[j:j+chunk_size].strip()
                            if chunk:
                                result.append(chunk)
                    else:
                        result.append(s)
        else:
            result.append(part)

    # If splitting produced only 1 segment (or 0), return as-is
    if len(result) <= 1:
        return [text.strip()]
    return result


def _random_trim_punctuation(segment):
    """Randomly drop trailing punctuation (~40% chance) for casual chat feel."""
    import re as _re
    if random.random() < 0.4:
        segment = _re.sub(r'[。！？，、….,!?]+$', '', segment)
    return segment.strip()
