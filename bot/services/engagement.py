"""LLM-driven engagement engine: whether, when, and how the bot joins a chat.

Replaces the fixed-cooldown + random-delay interjection with a two-stage
design: a cheap structured judge call decides respond/urgency/approach,
and only an affirmative judgment reaches the full persona reply pipeline.
All budget and safety gates stay in deterministic code.
"""

import asyncio
import json
from collections import deque
import logging
import random
import re
import time

from ..guard import is_blacklisted
from ..permission import is_group_enabled
from .topic_tracker import TRACKER

log = logging.getLogger("qqbot")

_JUDGE_SYSTEM = (
    "你在帮一个QQ群里的真人风格机器人判断要不要开口说话。像一个真人群友："
    "只在你能接梗、回答问题、提供信息或有真实情绪反应时才开口；"
    "别人聊得正好、话题与你无关、纯灌水时就安静看着。\n"
    "只输出一行JSON，不要任何其他文字：\n"
    '{"respond": true/false, "urgency": 0到1, '
    '"approach": "接梗|回答问题|吐槽|安慰|分享|表情包", '
    '"reason": "一句话依据", "delay_hint": "立刻|稍等|不急", '
    '"topic": "一行当前话题摘要"}'
)

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _engagement_config(config):
    return config.get("engagement", {}) if isinstance(config.get("engagement"), dict) else {}


def _group_state(dispatcher, group_id):
    states = getattr(dispatcher, "_engagement_states", None)
    if states is None:
        states = {}
        dispatcher._engagement_states = states
    return states.setdefault(int(group_id), {
        "pending_judge": False,
        "judge_times": deque(maxlen=64),
        "reply_date": "",
        "reply_count": 0,
        "next_judge_after": 0.0,
        "next_reply_after": 0.0,
        "judge_backoff": 1.0,
    })


def parse_judgment(text):
    """Parse the judge's JSON; any malformation degrades to 'stay silent'."""
    if not text:
        return None
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        urgency = float(data.get("urgency", 0))
    except (TypeError, ValueError):
        urgency = 0.0
    return {
        "respond": bool(data.get("respond")),
        "urgency": max(0.0, min(1.0, urgency)),
        "approach": str(data.get("approach") or "闲聊")[:12],
        "reason": str(data.get("reason") or "")[:120],
        "delay_hint": str(data.get("delay_hint") or "稍等"),
        "topic": str(data.get("topic") or "")[:120],
    }


def reply_delay_seconds(judgment):
    """Map the judgment to a human-like delay before speaking."""
    base = {"立刻": (3, 10), "稍等": (20, 60), "不急": (60, 150)}.get(
        judgment["delay_hint"], (20, 60))
    delay = random.uniform(*base)
    if judgment["urgency"] > 0.75:
        delay *= 0.5
    return delay


def _budget_ok(dispatcher, group_id, cfg, state, now):
    """Deterministic budget gates; the model never sees or overrides these."""
    judge_cap = int(cfg.get("judge_max_per_hour", 15) or 15)
    times = state["judge_times"]
    while times and now - times[0] > 3600:
        times.popleft()
    if len(times) >= judge_cap:
        return False
    today = time.strftime("%Y-%m-%d")
    if state["reply_date"] != today:
        state["reply_date"], state["reply_count"] = today, 0
    reply_cap = int(cfg.get("reply_max_per_day", 30) or 30)
    return state["reply_count"] < reply_cap


async def on_group_message(dispatcher, group_id, user_id, text, raw,
                           sender_card, message, message_id):
    """Observe one group message; maybe schedule a judgment."""
    cfg = _engagement_config(dispatcher.config)
    if not cfg.get("enabled", True):
        return
    try:
        signals = TRACKER.record_message(group_id, user_id, text, card=sender_card)
    except Exception as error:
        log.debug("Engagement tracker update failed: %s", error)
        return
    state = _group_state(dispatcher, group_id)
    now = time.time()
    if state["pending_judge"] or now < state["next_judge_after"]:
        return
    every = max(3, int(cfg.get("judge_every_messages", 8) or 8))
    triggered = bool(signals) or TRACKER.snapshot(group_id).get("streak_without_bot", 0) >= every
    if not triggered:
        return
    if not _budget_ok(dispatcher, group_id, cfg, state, now):
        return
    state["pending_judge"] = True
    state["judge_times"].append(now)
    dispatcher.create_background_task(
        _run_judgment(dispatcher, group_id, user_id, raw, sender_card,
                      message, message_id, signals),
        name="engagement-judge",
    )


async def _run_judgment(dispatcher, group_id, user_id, raw, sender_card,
                        message, message_id, signals):
    state = _group_state(dispatcher, group_id)
    try:
        judgment = await _call_judge(dispatcher, group_id, signals)
    except Exception as error:
        log.debug("Engagement judge call failed: %s", error)
        judgment = None
    now = time.time()
    cfg = _engagement_config(dispatcher.config)
    min_interval = max(10, int(cfg.get("judge_min_interval_seconds", 45) or 45))
    if judgment is None:
        # Silent degradation with bounded backoff: rather say nothing than
        # spam judgments when the provider is unhealthy.
        state["judge_backoff"] = min(state["judge_backoff"] * 2, 40)
        state["next_judge_after"] = now + min_interval * state["judge_backoff"]
        state["pending_judge"] = False
        return
    state["judge_backoff"] = 1.0
    state["next_judge_after"] = now + min_interval
    state["pending_judge"] = False
    TRACKER.update_topic_summary(group_id, judgment.get("topic"))
    _record_timeline(dispatcher, group_id, judgment)
    if not judgment["respond"]:
        return
    if now < state["next_reply_after"]:
        return
    dispatcher.create_background_task(
        _deliver_reply(dispatcher, group_id, user_id, raw, sender_card,
                       message, message_id, judgment),
        name="engagement-reply",
    )


async def _call_judge(dispatcher, group_id, signals):
    from ..ai.providers import _call_deepseek

    snapshot = TRACKER.snapshot(group_id)
    lines = TRACKER.recent_lines(group_id, limit=10)
    question = snapshot.get("open_question") or {}
    context = "\n".join([
        "信号: " + (", ".join(signals) if signals else "无"),
        "话题摘要: " + (snapshot.get("topic_summary") or "（暂无）"),
        "悬而未决的问题: " + (question.get("text", "无") if question else "无"),
        "bot已连续 {} 条没说话".format(snapshot.get("streak_without_bot", 0)),
        "",
        "最近消息:",
        "\n".join(lines) if lines else "（无）",
    ])
    cfg = _engagement_config(dispatcher.config)
    text = await _call_deepseek(
        dispatcher.config,
        [{"role": "system", "content": _JUDGE_SYSTEM},
         {"role": "user", "content": context}],
        max_tokens=int(cfg.get("max_tokens", 180) or 180),
        temperature=0.2,
        session=dispatcher.client.session,
    )
    return parse_judgment(text)


async def _deliver_reply(dispatcher, group_id, user_id, raw, sender_card,
                         message, message_id, judgment):
    await asyncio.sleep(reply_delay_seconds(judgment))
    # Final deterministic gates right before the side effect.
    if not is_group_enabled(dispatcher, group_id):
        return
    if is_blacklisted(group_id, user_id):
        return
    from ..ai import handle_ai_chat, _schedule_state
    schedule_state, _ = _schedule_state()
    if schedule_state == "sleep":
        return
    if not dispatcher._check_global_rate_limit():
        return
    allowed, _remaining = dispatcher._check_rate_limit(group_id)
    if not allowed:
        return
    import re as _re
    clean_raw = _re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()
    chat_ctx = dispatcher._build_chat_context(group_id)
    result = await handle_ai_chat(
        dispatcher, group_id, user_id, clean_raw, sender_card,
        chat_context=chat_ctx, message_id=message_id,
        reply_intent="自然接话：" + judgment["approach"],
        consecutive_replies=dispatcher._group_consecutive_replies.get(group_id, 0),
    )
    now = time.time()
    if result:
        state = _group_state(dispatcher, group_id)
        state["reply_count"] += 1
        # urgency replaces the old fixed cooldown: high-urgency replies allow
        # a quick follow-up, casual ones enforce a longer silence.
        state["next_reply_after"] = now + (45 if judgment["urgency"] > 0.7 else 150)
        TRACKER.record_bot_spoke(group_id, now)
        dispatcher._record_ai_outcome(group_id, True)
        dispatcher._record_rate_limit(group_id)
        dispatcher._record_global_rate_limit()


def _record_timeline(dispatcher, group_id, judgment):
    runtime = getattr(dispatcher, "agent_runtime", None)
    if runtime is None:
        return
    try:
        runtime.timeline.add(
            "group:{}".format(group_id), "engagement",
            "{} urgency={:.2f} approach={} reason={}".format(
                "开口" if judgment["respond"] else "静默",
                judgment["urgency"], judgment["approach"], judgment["reason"]),
            metadata={"group_id": int(group_id), "respond": judgment["respond"]})
    except Exception as error:
        log.debug("Engagement timeline record failed: %s", error)
