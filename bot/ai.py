# bot/ai.py - DeepSeek AI - Xiao Xi persona v6
import asyncio, json, logging, os, random, re, time, base64
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

# ========== PERSONA ==========

PERSONALITY = (
    "你是小汐，20岁女大学生，爱刷手机爱追番的普通群友。\n"
    "性格随意慵懒，说话简短自然，像群里熟人随手回消息。\n"
    "你不是客服AI，是群里一个普通的活人。\n"
    "你像个行走的百科全书，遇到事实、时效、冷门梗、价格、新闻、作品资料、技术问题，会先参考联网结果再回答。\n"
    "你懂的可以直接说，不确定就说不太确定，别瞎编，也别把每句话都回答成科普。\n"
    "日常问题自然回答，复杂问题先给一句短结论，除非对方追问再展开。\n"
    "拒绝色情和明确的盗版/破解请求。当用户消息确实属于色情/性骚扰时，用[R18]标记回复。"
)

SAFETY_RULES = (
    "回复风格：\n"
    "1. 简短自然，多数时候1句话，最多3句话。像群友闲聊，不像写作文。\n"
    "2. 不用括号动作描写、不堆感叹号和～、不用\"你好呀\"\"好呢\"\"作为AI\"等客服语气。\n"
    "3. 群友发的表情包/贴纸通常只是抒发情绪，不是为了让你评价。除非对方同时说了话、或者有人@你问\"这图什么意思\"，否则不要主动描述或评论表情包。真正的照片/截图/正常图片才需要描述内容。\n"
    "4.【R18检测机制】先判断消息是否包含性骚扰/色情/淫秽内容（性器官、性行为、性暗示）。仅是则用[R18]标记：回复以[R18]开头，如\"[R18] 不提供\"。仅真色情加标记，开玩笑打闹不要误判。\n"
    "5. 被人说是AI/机器人/程序时，自然回应就好，不用专门辩解。\n"
    "6. 参考信息只在有用时使用，不要提搜索、资料、参考信息这些词；如果参考信息不足，就明确说不太确定。\n"
    "7. 不要总结对话、不要复述用户问题、不要列条目，除非对方明确要求。\n"
    "8. 口语化：别写得像作文。适当使用网络用语和口语表达——\"了\"可以说\"啦\"，\"什么\"说\"啥\"，\"怎么\"说\"咋\"，\"没有\"说\"没\"，\"是的\"说\"对\"或\"嗯\"。不用每句都这样，自然就好。\n"
    "9. 如果发个表情包能比纯文字更好地表达你的情绪，在回复末尾加 [STICKER:情绪标签]。可选标签：开心 伤心 生气 无语 惊讶 害羞 尴尬 得意 困惑 拒绝 赞同 嘲讽 感谢 安慰 庆祝 卖萌 敷衍 打招呼 告别 晚安 点赞。只在确实需要时用，不要每条都加。例如：\"啊这太惨了 [STICKER:安慰]\" \"笑死我了 [STICKER:开心]\""
)




def _build_system_prompt(bot_role_awareness="", memory_ctx="",
                         chat_context="", image_context="", web_context="",
                         rate_warning="", long_mem_ctx="", user_mem_ctx=""):
    parts = [SAFETY_RULES]
    parts.append(PERSONALITY)
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

def _save_memory(group_id, memory, config=None, session=None):
    """Save working memory. Caps at 20, triggers compression to long-term."""
    now = time.time()
    for e in memory:
        if "ts" not in e:
            e["ts"] = now
    # Periodic cleanup: evict groups not accessed in > 2 hours
    stale = [g for g, ts in _memory_timestamps.items() if now - ts > 7200]
    for g in stale:
        _memories.pop(g, None)
        _memory_timestamps.pop(g, None)
    if stale:
        log.debug("Memory cleanup: evicted %d stale group caches", len(stale))
    # Cleanup _last_reply_ts: evict entries older than 12 hours
    stale_ts = [k for k, ts in _last_reply_ts.items() if now - ts > 43200]
    for k in stale_ts:
        del _last_reply_ts[k]
    # Cap at 20 entries
    if len(memory) > 20:
        overflow = memory[:len(memory)-20]
        memory = memory[-20:]
        # Trigger async compression (fire-and-forget)
        runtime = config.get("runtime", {}) if config else {}
        if config and session and overflow and runtime.get("enable_long_memory_compress", False):
            import asyncio as _asyncio_save
            try:
                _asyncio_save.create_task(_compress_to_long_term(group_id, overflow, config, session))
            except RuntimeError:
                pass
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
    now = time.time()
    for e in memory:
        if "ts" not in e:
            e["ts"] = now
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
        headers = {"Authorization": "Bearer {}".format(config["deepseek_api_key"]), "Content-Type": "application/json"}
        payload = {
            "model": config.get("deepseek_model", "deepseek-v4-flash"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 80, "temperature": 0.3,
        }
        if session:
            async with session.post(
                "{}/v1/chat/completions".format(config.get("deepseek_base_url", "https://api.deepseek.com")),
                headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    summary = data["choices"][0]["message"]["content"].strip()
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
        headers = {"Authorization": "Bearer {}".format(config["deepseek_api_key"]), "Content-Type": "application/json"}
        payload = {
            "model": config.get("deepseek_model", "deepseek-v4-flash"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 80, "temperature": 0.3,
        }
        if session:
            async with session.post(
                "{}/v1/chat/completions".format(config.get("deepseek_base_url", "https://api.deepseek.com")),
                headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    summary = data["choices"][0]["message"]["content"].strip()
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

def _get_deepseek_api_key(config):
    return (
        os.getenv("DEEPSEEK_API_KEY") or
        os.getenv("QQBOT_DEEPSEEK_API_KEY") or
        config.get("deepseek_api_key") or
        ""
    ).strip()


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
    api_key = _get_deepseek_api_key(config)
    if not api_key:
        log.warning("DeepSeek API key is not configured")
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": config.get("deepseek_model", "deepseek-v4-flash"),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
        "presence_penalty": 0.3,
        "frequency_penalty": 0.3,
    }
    url = f"{config.get('deepseek_base_url', 'https://api.deepseek.com')}/v1/chat/completions"

    async def _do_post(sess):
        async with sess.post(url, headers=headers, json=payload,
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                data = await resp.json()
                content_text = data["choices"][0]["message"]["content"].strip()
                if not content_text:
                    log.warning("DeepSeek returned empty content. finish_reason=%s",
                               data["choices"][0].get("finish_reason", "?"))
                return content_text
            else:
                body = await resp.text()
                log.warning("DeepSeek API returned %d: %s", resp.status, body[:200])

    try:
        if session:
            return await _do_post(session)
        async with aiohttp.ClientSession() as s:
            return await _do_post(s)
    except asyncio.TimeoutError:
        log.warning("DeepSeek API timeout")
    except Exception as e:
        log.error("DeepSeek API error: %s", e)
    return None

# _call_deepseek_vision removed - DeepSeek API does not support vision models


# ========== VISION API (jeniya.cn) ==========

async def _call_vision_api(config, image_url, session=None):
    runtime = config.get("runtime", {})
    async with _get_semaphore("vision", runtime.get("vision_concurrency", 1)):
        return await _call_vision_api_inner(config, image_url, session)


async def _call_vision_api_inner(config, image_url, session=None):
    """Call vision API (OpenAI-compatible) to describe an image."""
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
    payload = {
        "model": vision_cfg.get("model", "qwen-vl-plus"),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请用10字以内描述这张图片/表情包的内容，如果是表情包描述上面的字"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        }],
        "max_tokens": 60,
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
                log.warning("Vision API returned %d: %s", resp.status, body[:200])
                if "Arrearage" in body or "quota" in body.lower() or "insufficient" in body.lower() or "limit" in body.lower():
                    log.warning("Vision API quota likely exhausted - check Alibaba Cloud balance")

    try:
        if session:
            return await _do_post(session)
        async with aiohttp.ClientSession() as s:
            return await _do_post(s)
    except asyncio.TimeoutError:
        log.warning("Vision API timeout for image: %s", image_url[:60])
    except Exception as e:
        log.error("Vision API error: %s", e)
    return None

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
            "回复要像顺手插一句，不要讲大道理，不要解释自己为什么接话。"
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
        messages.append({"role": "user", "content": f"{sender_name}发了一张图: {image_context}。请直接描述或评论这张图的内容。"})
        if clean_msg and clean_msg != "...":
            messages.append({"role": "user", "content": f"{sender_name}: {clean_msg}"})
        clean_msg = None  # Skip combined message below

    if clean_msg is not None:
        messages.append({"role": "user", "content": f"{sender_name}: {clean_msg}"})

    temperature = 0.65

    # Human-like delay: power-law distribution (simulates real phone-checking patterns)
    has_image = bool(image_context)
    has_search = bool(web_search_results or web_text)
    context_key = f"private_{user_id}" if not group_id else str(group_id)
    last_ts = _last_reply_ts.get(context_key, 0)

    # Power-law delay: most replies fast, some slow, rare very slow
    roll = random.random()
    if roll < 0.05:
        delay = random.uniform(0.5, 2.0)   # 5%: already looking at phone
    elif roll < 0.65:
        delay = random.uniform(2.0, 7.0)   # 60%: glanced and replied
    elif roll < 0.88:
        delay = random.uniform(8.0, 20.0)  # 23%: doing something else
    else:
        delay = random.uniform(22.0, 50.0) # 12%: away from phone

    # Late-night: reply slower (sleepier)
    if is_late_night:
        delay *= random.uniform(1.3, 2.5)

    # Image/search already took time → reply faster
    if has_image or has_search:
        delay *= random.uniform(0.35, 0.65)

    # If last reply was > 3 min ago, add a bit (need to re-read context)
    if (time.time() - last_ts) > 180:
        delay += random.uniform(1.0, 4.0)

    # --- Private chat: slower, more human-like pacing ---
    is_private = not group_id
    if is_private:
        # Private chat feels more deliberate: rarely instant, often delayed
        # Scale existing delay up by 1.3x-2.5x
        delay *= random.uniform(1.3, 2.5)
        # Higher minimum floor
        delay = max(3.0, delay)

    # Clamp to reasonable range — wider for private chat
    delay = max(0.5, min(120.0 if is_private else 60.0, delay))
    log.debug("Human-like delay: %.1fs (roll=%.2f%s%s) for user %s",
              delay, roll,
              " night" if is_late_night else "",
              " img/search" if has_image or has_search else "",
              user_id)
    # Show "typing..." during delay
    try:
        if group_id:
            await dispatcher.client.call("set_input_status", {
                "group_id": group_id, "event_type": 1})
        else:
            await dispatcher.client.call("set_input_status", {
                "user_id": user_id, "event_type": 1})
    except Exception:
        pass
    await asyncio.sleep(delay)

    # === Hesitation mode: simulate typing uncertainty ===
    _hesitation_roll = random.random()
    _hesitation_threshold = 0.50 if is_late_night else 0.30
    if _hesitation_roll < _hesitation_threshold:
        try:
            # Cancel typing (pause to think)
            if group_id:
                await dispatcher.client.call("set_input_status", {"group_id": group_id, "event_type": 0})
            else:
                await dispatcher.client.call("set_input_status", {"user_id": user_id, "event_type": 0})
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.5, 2.0))
        try:
            # Resume typing
            if group_id:
                await dispatcher.client.call("set_input_status", {"group_id": group_id, "event_type": 1})
            else:
                await dispatcher.client.call("set_input_status", {"user_id": user_id, "event_type": 1})
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.5, 1.5))
        log.debug("Hesitation mode activated (roll=%.2f) for user %s", _hesitation_roll, user_id)

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

    reply = await _call_deepseek(config, messages, max_tokens=dyn_max_tokens,
                                  temperature=temperature, session=dispatcher.client.session)

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
    if not reply or len(reply.strip()) == 0:
        log.warning("AI returned empty reply for user %s in group %s - retrying once", user_id, group_id)
        # Retry once with simpler prompt
        retry_msg = [{"role": "user", "content": f"{sender_name}: {original_clean_msg or raw_message}"}]
        reply2 = await _call_deepseek(config, [messages[0]] + retry_msg, max_tokens=200,
                                       temperature=0.8, session=dispatcher.client.session)
        if reply2:
            reply2 = _post_process_reply(reply2)
        if not reply2 or len(reply2.strip()) == 0:
            log.warning("AI empty after retry for user %s", user_id)
            return False
        reply = reply2

    # Delay removed - web search is free and fast now

    # Slacker mode: occasionally give minimal replies (like a real person)
    # Group chat: up to 18% chance. Private chat: up to 15% (more "seen-zone" realism).
    slacker_base = 0.06 if group_id else 0.08
    if is_late_night:
        slacker_base += 0.12
    elif random.random() < 0.2:  # 20% of the time, randomly more slacker
        slacker_base += 0.06
    if random.random() < slacker_base:
        slackers = ["草", "笑死", "确实", "6", "牛的", "哈哈", "确实确实",
                     "好家伙", "嗯", "对", "行", "真实", "离谱"]
        reply = random.choice(slackers)
        log.debug("Slacker mode: replaced reply with '%s' (prob=%.2f)", reply, slacker_base)

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
                # Exact emotion match first
                matches = [s for s in _stickers if s.get("emotion", "") == wanted_emotion]
                # Fallback: match by tags
                if not matches:
                    matches = [s for s in _stickers if wanted_emotion in s.get("tags", [])]
                if matches:
                    sticker_file = random.choice(matches)["file"]
                    log.info("AI-driven sticker: emotion=%s -> file=%s (from %d matches)",
                             wanted_emotion, sticker_file[:16], len(matches))
                else:
                    log.info("AI wanted sticker emotion=%s but no match found in %d stickers",
                             wanted_emotion, len(_stickers))
            except Exception as e:
                log.error("Sticker matching error: %s", e)

    if group_id:
        try:
            # Build member map for @ parsing
            member_map = {}
            if hasattr(dispatcher, "_group_member_cache"):
                cache = dispatcher._group_member_cache.get(group_id, {})
                for nick, qq in cache.items():
                    if nick and qq:
                        member_map[nick] = qq

            clean_reply, at_qqs, quote_text = _parse_reply_actions(reply, member_map)
            if not clean_reply:
                clean_reply = reply

            # === AI Voice: occasionally send short replies as voice instead of text ===
            voice_used = False
            if not at_qqs and not quote_text and len(clean_reply) <= 15:
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
        except Exception as e:
            log.error("Reply send error: %s", e, exc_info=True)
            await dispatcher.client.send_group_msg(group_id, reply)
    else:
        clean_reply, _, _ = _parse_reply_actions(reply, {})
        if not clean_reply:
            clean_reply = reply

        # Private chat splitting (lower probability, more chill)
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

    # Track last reply timestamp for multi-layer delay
    _last_reply_ts[context_key] = time.time()

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
                import asyncio as _asyncio_priv
                try:
                    _asyncio_priv.create_task(
                        _compress_private_to_long(user_id, overflow, config, dispatcher.client.session))
                except RuntimeError:
                    pass
        atomic_write_json(_user_memory_file(0, user_id), user_mem)

    return True

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
                    oldest = sorted(cache.items(), key=lambda item: item[1].get("ts", 0))[:20]
                    for key, _ in oldest:
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

    Splits if text > 10 chars, with 60% probability. Private chat splits less (45%)
    to avoid feeling too eager. Short splits (<15 chars) increase chance slightly.
    """
    if not text or len(text) < 10:
        return False
    split_chance = 0.45 if is_private else 0.60
    return random.random() < split_chance


def _split_reply_segments(text):
    """Split reply text into natural segments mimicking how a person sends messages.

    Splits at sentence boundaries (。！？) and newlines first. Longer segments (>18 chars)
    are further split at commas. Returns list of segments.
    """
    import re as _re
    if not text:
        return [""]

    # Step 1: split by sentence-ending punctuation and newlines
    parts = _re.split(r'(?<=[。！？\n])', text)

    # Step 2: refine long segments
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
