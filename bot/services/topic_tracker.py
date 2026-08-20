"""Per-group rolling conversation state for the engagement engine.

Pure code, zero API cost: every observed group message updates a small
state object; the engagement judge reads it to decide whether to speak.
"""

import re
import time
from collections import deque

_QUESTION_RE = re.compile(
    r"[?？]|吗[。!！\s]*$|呢[。!！\s]*$|怎么|为什么|咋|求问|请问|有人知道|谁会|谁来")
_EMOTION_RE = re.compile(r"哈{2,}|2333|笑死|awsl|泪目|绷不住|绝了|好耶|芜湖")

_MAX_GROUPS = 200


class TopicTracker:
    """Rolling per-group conversation state."""

    def __init__(self, max_messages=12):
        self._max_messages = max_messages
        self._groups = {}

    def _state(self, group_id):
        group_id = int(group_id)
        state = self._groups.get(group_id)
        if state is None:
            if len(self._groups) >= _MAX_GROUPS:
                # Evict the least recently active group.
                oldest = min(self._groups.items(),
                             key=lambda item: item[1].get("last_ts", 0))
                self._groups.pop(oldest[0], None)
            state = {
                "messages": deque(maxlen=self._max_messages),
                "topic_summary": "",
                "bot_last_spoke": 0.0,
                "streak_without_bot": 0,
                "open_question": None,
                "emotion_hits": deque(maxlen=40),
                "last_ts": 0.0,
            }
            self._groups[group_id] = state
        return state

    def record_message(self, group_id, user_id, text, card="", now=None):
        """Update state with one observed message; return triggered signals."""
        now = time.time() if now is None else now
        state = self._state(group_id)
        text = (text or "").strip()
        state["messages"].append({
            "user_id": int(user_id or 0), "card": str(card or "")[:24],
            "text": text[:200], "ts": now,
        })
        state["last_ts"] = now
        state["streak_without_bot"] = int(state.get("streak_without_bot", 0)) + 1

        signals = []
        question = state.get("open_question")
        if _QUESTION_RE.search(text):
            state["open_question"] = {
                "user_id": int(user_id or 0), "text": text[:120], "ts": now,
            }
            signals.append("question_open")
        elif question:
            age = now - float(question.get("ts", 0))
            if age > 300:
                state["open_question"] = None  # too stale to be actionable
            elif age > 90:
                # Cheap heuristic cannot tell answers from chatter, so an
                # old unresolved question simply keeps signaling.
                signals.append("question_unanswered")

        if _EMOTION_RE.search(text):
            hits = state["emotion_hits"]
            hits.append(now)
            recent = [ts for ts in hits if now - ts <= 60]
            if len(recent) >= 3:
                signals.append("emotion_spike")
        return signals

    def record_bot_spoke(self, group_id, now=None):
        now = time.time() if now is None else now
        state = self._state(group_id)
        state["bot_last_spoke"] = now
        state["streak_without_bot"] = 0

    def velocity(self, group_id, window=300, now=None):
        now = time.time() if now is None else now
        state = self._groups.get(int(group_id))
        if not state:
            return 0.0
        count = sum(1 for m in state["messages"] if now - m["ts"] <= window)
        return count / (window / 60.0)

    def recent_lines(self, group_id, limit=10):
        state = self._groups.get(int(group_id))
        if not state:
            return []
        lines = []
        for m in list(state["messages"])[-limit:]:
            who = m["card"] or str(m["user_id"])
            lines.append("{}: {}".format(who, m["text"]))
        return lines

    def snapshot(self, group_id):
        state = self._groups.get(int(group_id))
        if not state:
            return {}
        question = state.get("open_question")
        return {
            "topic_summary": state.get("topic_summary", ""),
            "streak_without_bot": state.get("streak_without_bot", 0),
            "bot_last_spoke": state.get("bot_last_spoke", 0.0),
            "open_question": question,
        }

    def update_topic_summary(self, group_id, summary):
        if summary:
            self._state(group_id)["topic_summary"] = str(summary)[:120]


TRACKER = TopicTracker()
