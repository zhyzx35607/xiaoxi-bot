# bot/ai.py - DeepSeek AI - Xiao Xi persona v6
import asyncio, json, logging, os, random, re, time, base64
from collections import deque
from datetime import datetime, timezone, timedelta
import aiohttp, urllib.parse
from ..utils import atomic_write_json
from .reply import (
    _build_group_reply_segments,
    _parse_reply_actions,
    _parse_reply_tags,
    _prepare_group_reply,
)
from .prompts import (
    OUTPUT_PROTOCOL,
    PERSONALITY,
    SAFETY_RULES,
    TOOL_USAGE_RULES,
    _build_system_prompt,
    _schedule_state,
    _split_reply_lines,
    _typing_delay_secs,
)
from .search import (
    _format_uapi_search_results,
    _parse_bing_results,
    _search_web_bing,
    _search_web_uapi,
    search_web,
)
from .memory import (
    _is_repetitive,
    _last_reply_ts,
    _load_long_memory,
    _load_memory,
    _load_user_long_memory,
    _load_user_memory,
    _record_reply,
    _save_memory,
    _save_user_memory,
    clear_group_memory,
    clear_user_memory,
)
from .providers import (
    _call_deepseek,
    _call_deepseek_inner,
    _call_vision_api,
    _get_semaphore,
    _providers_support_tools,
    format_ai_provider_status,
    generate_image,
    get_ai_provider_status,
    is_ai_busy,
)
from .stickers import (
    STICKER_DIR,
    _allow_sticker_send,
    _build_sticker_inventory,
    collect_sticker_async,
    describe_image,
    get_sticker_summaries,
)
from .tools import (
    _chat_with_tools,
    _maybe_call_napcat_tool,
    _should_consider_napcat_tool,
)
log = logging.getLogger("qqbot")
# ========== MEMORY ==========
# ========== USER-SPECIFIC MEMORY (per person per group) ==========
# ========== LONG-TERM GROUP MEMORY ==========
# ========== PER-USER LONG-TERM MEMORY ==========
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
# ========== IMAGE GENERATION ==========
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
                          reply_intent="", consecutive_replies=0,
                          interaction_allowed=False):
    config = dispatcher.config
    context_key = f"private_{user_id}" if not group_id else str(group_id)
    bot_role = ""
    if group_id:
        try:
            from ..permission import get_bot_role
            _, role_display = await get_bot_role(dispatcher, group_id)
            if role_display != "member":
                bot_role = f"你是本群的{role_display}，作为管理员要以身作则友好交流。"
        except Exception:
            pass
    memory = _load_memory(group_id, config) if group_id else []
    
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
        u_long = _load_user_long_memory(group_id, user_id) if user_id else []
        if u_long:
            u_long_lines = ["- " + e["content"][:120] for e in u_long[-5:]]
            if u_long_lines:
                long_mem_ctx += "\n\n【你和 {} 之前聊过的长期话题】\n".format(
                    sender_name if sender_name else "此人") + "\n".join(u_long_lines)
    else:
        long_mem = _load_user_long_memory(0, user_id) if user_id else []
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
    # Tiered tools for native function calling: explicit scenes (interaction_allowed)
    # get all three tiers; interjection scenes get the read tier only.
    from ai_tools import build_tool_schemas
    tools = build_tool_schemas(explicit=interaction_allowed)
    system_prompt = _build_system_prompt(
        bot_role_awareness=bot_role,
        memory_ctx=mem_ctx,
        chat_context=chat_context if group_id else "",
        image_context=image_context,
        web_context=web_text,
        rate_warning=rate_warning,
        long_mem_ctx=long_mem_ctx,
        user_mem_ctx=user_mem_ctx,
        tool_ctx=TOOL_USAGE_RULES if tools else "",
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
    temperature = 0.65
        # Fixed token budget: resource boundary, not behavior control
    is_question = bool(clean_msg) and ("?" in str(clean_msg) or "？" in str(clean_msg) or
                    any(w in str(clean_msg) for w in ("怎么", "为什么", "如何", "啥", "什么")))
    if group_id:
        dyn_max_tokens = 450 if is_question else 400
    else:
        dyn_max_tokens = 500
    # Light pre-call delay simulates "reading the message"
    is_private = not group_id
    pre_delay = random.uniform(0.2, 0.8) if is_private else random.uniform(0.3, 1.0)
    async def _delayed_ai_request():
        await asyncio.sleep(pre_delay)
        # Native function calling: explicit scenes get all tool tiers,
        # interjections get the read tier only. One semaphore hold per loop.
        if tools:
            tool_reply = await _chat_with_tools(
                dispatcher, messages, tools, group_id, user_id,
                message_id=message_id, interaction_allowed=interaction_allowed,
                max_tokens=dyn_max_tokens, temperature=temperature)
            if tool_reply is not None:
                return tool_reply
            # Provider rejected tools or loop failed: legacy keyword-gated
            # JSON tool loop, then a plain completion.
            if _should_consider_napcat_tool(original_clean_msg or raw_message):
                tool_result = await _maybe_call_napcat_tool(
                    dispatcher, group_id, user_id, original_clean_msg or raw_message,
                    chat_context, interaction_allowed=interaction_allowed,
                    message_id=message_id)
                if tool_result:
                    messages.append({"role": "system",
                                     "content": "【工具查询结果】\n" + tool_result})
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
    if reply and "[R18]" in reply:
        log.warning("AI rejected user %s in group %s: %s", user_id, group_id, reply[:50])
        owner = config.get("bot_owner")
        bot_qq = config.get("bot_qq")
        if user_id == owner or user_id == bot_qq:
            log.info("Skipping R18 escalation for bot owner/self")
        else:
            from ..guard import add_warning, get_warning_count, add_blacklist
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
    if not reply or not reply.strip():
        log.warning("AI returned empty reply for user %s in group %s", user_id, group_id)
        await _notify_ai_unavailable(
            dispatcher, group_id, user_id,
            explicit=(not group_id or reply_intent == "直接回应"),
        )
        return False
    # === AI-driven sticker: parse [STICKER:xxx] tag ===
    wanted_emotion = None
    _sticker_match = re.search(r'\[STICKER:([^\]]+)\]', reply)
    if _sticker_match:
        wanted_emotion = _sticker_match.group(1).strip()
        reply = reply.replace(_sticker_match.group(0), '').strip()
    sticker_file = None
    if wanted_emotion:
        _sticker_path = os.path.join(STICKER_DIR,
            f"private_{user_id}.json" if not group_id else f"group_{group_id}.json")
        if os.path.exists(_sticker_path):
            try:
                with open(_sticker_path, encoding="utf-8") as _sf:
                    _stickers = json.load(_sf)
                exact_matches = [s for s in _stickers if s.get("emotion", "") == wanted_emotion]
                current_gid = str(group_id) if group_id else f"private_{user_id}"
                same_group = [s for s in exact_matches if s.get("group_id", "") == current_gid]
                matches = same_group if same_group else exact_matches
                if not matches:
                    tag_matches = [s for s in _stickers if wanted_emotion in s.get("tags", [])]
                    same_group_tag = [s for s in tag_matches if s.get("group_id", "") == current_gid]
                    matches = same_group_tag if same_group_tag else tag_matches
                if matches:
                    sticker_file = random.choice(matches)["file"]
                    if not _allow_sticker_send(config, group_id, user_id):
                        sticker_file = None
                    log.info("AI-driven sticker: emotion=%s -> file=%s (from %d matches, same_group=%s)",
                             wanted_emotion, (sticker_file or "")[:16], len(matches),
                             bool(same_group or same_group_tag))
                else:
                    log.info("AI wanted sticker emotion=%s but no match found in %d stickers",
                             wanted_emotion, len(_stickers))
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
            clean_reply, at_qqs, quote_text, poke_targets = _prepare_group_reply(
                reply, member_map, user_id=user_id, message_id=message_id)
            # Typing delay proportional to reply length
            await asyncio.sleep(_typing_delay_secs(clean_reply))
            segments = _split_reply_lines(clean_reply) or [clean_reply]
            voice_sent = False
            if (len(segments) == 1 and not at_qqs and not quote_text
                    and not poke_targets and not sticker_file):
                from ..services.voice_reply import maybe_send_short_voice
                voice_sent = await maybe_send_short_voice(
                    dispatcher, group_id, segments[0].strip())
            if not voice_sent:
                for i, seg_text in enumerate(segments):
                    seg_text = seg_text.strip()
                    if not seg_text:
                        continue
                    _segs = _build_group_reply_segments(
                        seg_text, at_qqs if i == 0 else ())
                    if i == len(segments) - 1 and sticker_file:
                        _segs.append({"type": "image", "data": {"file": sticker_file}})
                    if quote_text and message_id and i == 0:
                        await dispatcher.client.send_group_msg_reply(group_id, _segs, message_id)
                    else:
                        await dispatcher.client.send_group_msg(group_id, _segs)
                    if i < len(segments) - 1:
                        await asyncio.sleep(random.uniform(0.5, 2.0))
                log.debug("Split reply into %d segments for group %s", len(segments), group_id)
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
        await asyncio.sleep(_typing_delay_secs(clean_reply))
        segments = _split_reply_lines(clean_reply) or [clean_reply]
        try:
            for i, seg_text in enumerate(segments):
                seg_text = seg_text.strip()
                if not seg_text:
                    continue
                _segs = [{"type": "text", "data": {"text": seg_text}}]
                if i == len(segments) - 1 and sticker_file:
                    _segs.append({"type": "image", "data": {"file": sticker_file}})
                await dispatcher.client.send_private_msg(user_id, _segs)
                if i < len(segments) - 1:
                    await asyncio.sleep(random.uniform(0.5, 2.0))
            log.debug("Split private reply into %d segments for user %s", len(segments), user_id)
        except Exception as e:
            log.error("Private reply send error (sticker may be stale): %s", e)
            await dispatcher.client.send_private_msg(user_id, clean_reply or reply)
    # Track last reply timestamp for multi-layer delay
    _last_reply_ts[context_key] = time.time()
    # Track reply content for anti-echo
    if user_id:
        _record_reply(user_id, clean_reply if clean_reply else reply)
    # Learn from conversation & save memory
    from ..memory import extract_user_info
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
        _save_user_memory(group_id, user_id, user_mem, config, dispatcher.client.session)
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
        # === Private chat memory (deeper: 30 entries + LLM long-term compression) ===
        user_mem = _load_user_memory(0, user_id)
        for info in learned:
            user_mem.append({"role": "system", "content": info, "ts": now})
        user_mem.append({"role": "user", "content": "{}: {}".format(sender_name, user_msg_text), "ts": now})
        user_mem.append({"role": "assistant", "content": reply, "ts": now})
        _save_user_memory(0, user_id, user_mem, config,
                          dispatcher.client.session, max_entries=30)
    return True







# ========== REPLY PARSING ==========




# ========== IMAGE DESCRIPTION (识图) ==========
import re as _re_sticker
# ---- Collect sticker - now with vision analysis ----
# ---- Best sticker picker ----
# ---- Sticker summaries for /list ----
# ========== WEB SEARCH ==========
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
