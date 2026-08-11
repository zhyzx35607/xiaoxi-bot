"""Owner-only proactive companion orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from ..ai.providers import _call_deepseek
from ..memory import contains_sensitive_data, sanitize_for_memory
from .companion_store import CompanionStore

log = logging.getLogger("qqbot")

_DEFAULT_STATE = {
    "mood": "calm", "valence": 0.1, "energy": 0.5, "attachment": 0.5,
    "concern": 0.0, "last_interaction_at": 0.0, "last_outgoing_at": 0.0,
    "active_topics": [], "current_persona": "", "chat_id": "",
    "proactive_enabled": True, "followup_enabled": True, "media_enabled": True,
    "muted_until": 0.0, "last_decision_at": 0.0,
}


def _clamp(value, low=0.0, high=1.0):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


class CompanionRuntime:
    def __init__(self, config, root):
        self.config = config
        self.owner_id = int(config.get("bot_owner") or 0)
        self.store = CompanionStore(root)
        self._lock = asyncio.Lock()
        self._migrate_legacy(Path(root))

    def _migrate_legacy(self, agent_root: Path):
        if not self.owner_id:
            return
        state = self.store.get_state(self.owner_id, _DEFAULT_STATE)
        if state.get("legacy_memory_migrated"):
            return
        candidates = [
            agent_root.parent / "memories" / "group_0_u{}.json".format(self.owner_id),
            agent_root.parent / "memories" / "private_{}_long.json".format(self.owner_id),
            agent_root / "memory" / "owner_{}".format(self.owner_id) / "confirmed.json",
        ]
        imported = 0
        for path in candidates:
            try:
                if not path.exists():
                    continue
                rows = json.loads(path.read_text(encoding="utf-8"))
                for row in rows if isinstance(rows, list) else []:
                    content = str(row.get("content") if isinstance(row, dict) else row).strip()
                    if not content or contains_sensitive_data(content):
                        continue
                    self.store.add_episode(
                        self.owner_id, sanitize_for_memory(content),
                        tags=["legacy_memory"], source=str(path.name))
                    imported += 1
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                log.warning("Legacy companion memory migration skipped path=%s: %s", path, error)
        state["legacy_memory_migrated"] = True
        state["legacy_memory_imported"] = imported
        self.store.save_state(self.owner_id, state)

    def state(self):
        return self.store.get_state(self.owner_id, _DEFAULT_STATE)

    def set_control(self, field: str, value):
        state = self.state()
        if field in {"proactive_enabled", "followup_enabled", "media_enabled"}:
            state[field] = bool(value)
        elif field == "muted_until":
            state[field] = float(value or 0)
        self.store.save_state(self.owner_id, state)
        return state

    def _decay(self, state, now):
        previous = float(state.get("updated_at", state.get("last_interaction_at", now)) or now)
        hours = max(0.0, (now - previous) / 3600.0)
        factor = max(0.0, min(1.0, hours / 24.0))
        state["valence"] = 0.5 + (float(state.get("valence", 0.5)) - 0.5) * (1 - factor * 0.25)
        state["energy"] = 0.5 + (float(state.get("energy", 0.5)) - 0.5) * (1 - factor * 0.4)
        state["concern"] = max(0.0, float(state.get("concern", 0.0)) - factor * 0.03)
        state["updated_at"] = now

    def _apply_delta(self, state, delta: dict, now):
        if not isinstance(delta, dict):
            return
        mood = str(delta.get("mood") or "").strip()[:40]
        if mood:
            state["mood"] = mood
        for key in ("valence", "energy", "attachment", "concern"):
            if key in delta:
                state[key] = _clamp(delta[key])
        state["updated_at"] = now

    def observe_owner_message(self, text: str, *, timestamp=None, source="message"):
        now = float(timestamp or time.time())
        text = sanitize_for_memory(text).strip()[:4000]
        if not text:
            return self.state()
        state = self.state()
        self._decay(state, now)
        state["last_interaction_at"] = now
        state["concern"] = max(0.0, state.get("concern", 0.0) - 0.08)
        state["energy"] = _clamp(state.get("energy", 0.5) + 0.04)
        state["attachment"] = _clamp(state.get("attachment", 0.5) + 0.015)
        self.store.cancel_followups(self.owner_id)
        normalized = re.sub(r"[，。！？,.!\s]", "", text.lower())
        if any(marker in normalized for marker in ("别追问", "不要追问", "停止追问")):
            state["followup_enabled"] = False
        if any(marker in normalized for marker in ("可以追问", "恢复追问")):
            state["followup_enabled"] = True
        if any(marker in normalized for marker in ("暂停媒体", "别发图片", "不要发视频")):
            state["media_enabled"] = False
        if any(marker in normalized for marker in ("恢复媒体", "可以发图片", "可以发视频")):
            state["media_enabled"] = True
        if any(marker in normalized for marker in ("停止主动", "别主动", "不要主动")):
            state["proactive_enabled"] = False
        if any(marker in normalized for marker in ("恢复主动", "可以主动", "继续主动")):
            state["proactive_enabled"] = True
        if not contains_sensitive_data(text):
            self.store.add_episode(self.owner_id, "主人：" + text, tags=["owner_message"], source=source)
        self._extract_structured_facts(text, now)
        self.store.add_emotion_snapshot(self.owner_id, state)
        self.store.save_state(self.owner_id, state)
        return state

    def observe_outgoing(self, text: str, *, topic="", timestamp=None):
        now = float(timestamp or time.time())
        state = self.state()
        self._decay(state, now)
        state["last_outgoing_at"] = now
        if topic:
            topics = [item for item in state.get("active_topics", []) if item != topic]
            state["active_topics"] = (topics + [topic])[-12:]
        state["updated_at"] = now
        if text and not contains_sensitive_data(text):
            self.store.add_episode(self.owner_id, "小汐：" + sanitize_for_memory(text), tags=["assistant_message", topic] if topic else ["assistant_message"], source="outbox")
        self.store.add_emotion_snapshot(self.owner_id, state)
        self.store.save_state(self.owner_id, state)
        return state

    def _extract_structured_facts(self, text: str, now: float):
        birthday = re.search(r"(?:我(?:的)?生日(?:是|为)?|生日(?:是|为)?)\s*(\d{1,2})\s*(?:[./-]|月)\s*(\d{1,2})\s*日?", text)
        if not birthday:
            birthday = re.search(r"(\d{1,2})\s*(?:[./-]|月)\s*(\d{1,2})\s*日?\s*(?:是)?我(?:的)?生日", text)
        if birthday:
            month, day = int(birthday.group(1)), int(birthday.group(2))
            try:
                datetime(2000, month, day)
                valid_date = True
            except ValueError:
                valid_date = False
            if valid_date:
                title = "主人生日"
                value = {"month": month, "day": day}
                self.store.upsert_fact(self.owner_id, "birthday", "owner_birthday", f"{month}月{day}日是主人的生日", value, "owner_message", 0.98)
                self.store.upsert_event(self.owner_id, "birthday", title, month, day, "yearly", value)
        patterns = (
            (r"(?:我喜欢|我偏好|我爱吃|我常用)\s*(.{2,80})", "preference"),
            (r"(?:记住|记得|以后记住)\s*[：:]?\s*(.{2,160})", "commitment"),
        )
        for pattern, category in patterns:
            match = re.search(pattern, text)
            if match and not contains_sensitive_data(match.group(1)):
                content = match.group(1).strip()
                key = re.sub(r"\W+", "_", content.lower()).strip("_")[:100] or category
                self.store.upsert_fact(self.owner_id, category, key, content, content, "owner_message", 0.82)

    def context(self, query=""):
        state = self.state()
        facts = self.store.list_facts(self.owner_id, query=query, limit=30)
        episodes = self.store.search_episodes(self.owner_id, query=query, limit=8)
        lines = [
            "当前内部状态：心情={} valence={:.2f} energy={:.2f} attachment={:.2f} concern={:.2f}".format(
                state.get("mood", "calm"), float(state.get("valence", 0.5)), float(state.get("energy", 0.5)),
                float(state.get("attachment", 0.5)), float(state.get("concern", 0.0))),
        ]
        if facts:
            lines.append("长期事实：" + "；".join(str(item.get("content", ""))[:180] for item in facts[:16]))
        if episodes:
            lines.append("近期经历：" + "；".join(str(item.get("content", ""))[:180] for item in reversed(episodes[:6])))
        return "\n".join(lines)[:10000]

    def _due_reason(self, now):
        state = self.state()
        followups = self.store.due_followups(self.owner_id, now)
        if followups and state.get("followup_enabled", True):
            return "followup", followups[0]
        dt = datetime.fromtimestamp(now)
        for event in self.store.due_events(self.owner_id, dt.month, dt.day):
            key = str(dt.year)
            if event.get("last_trigger_key") != key:
                return "event", event
        bucket = dt.strftime("%Y%m%d%H")
        if dt.hour == 20 and state.get("last_time_bucket") != bucket:
            return "time", {"topic": "time-checkin", "hour": dt.hour}
        last_interaction = float(state.get("last_interaction_at", 0) or 0)
        last_outgoing = float(state.get("last_outgoing_at", 0) or 0)
        idle = now - max(last_interaction, last_outgoing)
        idle_threshold = max(8 * 3600, int(
            self.config.get("agent", {}).get("companion_idle_seconds", 8 * 3600)))
        if idle >= idle_threshold:
            return "idle", {"topic": "idle-companion", "idle_seconds": idle}
        return "none", None

    async def decide(self, dispatcher, *, now=None, force=False):
        if not self.owner_id:
            return None
        now = float(now or time.time())
        state = self.state()
        if not self.config.get("agent", {}).get("enabled", True):
            return None
        if not state.get("proactive_enabled", True) or float(state.get("muted_until", 0) or 0) > now:
            return None
        reason, payload = self._due_reason(now)
        if reason == "none" and not force:
            return None
        settings = dispatcher.config.get("agent", {})
        if not force:
            min_gap = max(21600, int(settings.get("companion_min_gap_seconds", 21600)))
            if now - float(state.get("last_outgoing_at", 0) or 0) < min_gap and reason not in {"event", "followup"}:
                return None
        roleplay_hint = ""
        try:
            roleplay = getattr(dispatcher, "roleplay", None)
            if roleplay is not None and roleplay.enabled():
                roleplay_hint = roleplay.status(self.owner_id, None)
        except Exception as error:
            log.debug("Companion roleplay state unavailable: %s", error)
        prompt = self._build_prompt(reason, payload, now, roleplay_hint)
        async with self._lock:
            raw = await _call_deepseek(dispatcher.config, [{"role": "user", "content": prompt}],
                                       max_tokens=int(settings.get("companion_max_tokens", 700)),
                                       temperature=float(settings.get("companion_temperature", 0.85)),
                                       session=getattr(dispatcher.client, "session", None))
        result = self._parse_result(raw)
        if result is None and reason == "event" and payload:
            result = {
                "should_send": True, "priority": "urgent",
                "topic": payload.get("title", "important-event"),
                "message_parts": ["主人，今天是{}，我记得这个重要日子。祝你今天开心，生日快乐！".format(payload.get("title", "你的特别日子"))],
                "emotion_delta": {"mood": "excited", "valence": 0.9, "energy": 0.8},
                "memory_candidates": [], "followup": {"enabled": True, "max_attempts": 3}, "media_request": {},
            }
        if not result or not result.get("message_parts"):
            return None
        if reason not in {"event", "followup"} and str(
                result.get("priority") or "normal") != "urgent":
            result["message_parts"] = result["message_parts"][:1]
        if reason != "event":
            result["message_parts"] = [part[:120] for part in result["message_parts"]]
        self._apply_delta(state, result.get("emotion_delta") or {}, now)
        state["last_decision_at"] = now
        if reason == "time":
            state["last_time_bucket"] = datetime.fromtimestamp(now).strftime("%Y%m%d%H")
        self.store.save_state(self.owner_id, state)
        topic = str(result.get("topic") or (payload or {}).get("topic") or reason)[:160]
        priority = str(result.get("priority") or ("urgent" if reason == "event" else "normal"))
        key = "{}:{}:{}".format(topic, datetime.fromtimestamp(now).strftime("%Y%m%d%H"), reason)
        self.store.enqueue(self.owner_id, topic, {"message_parts": result["message_parts"], "media_request": result.get("media_request") or {}}, now, priority, key)
        for candidate in result.get("memory_candidates") or []:
            if isinstance(candidate, dict) and candidate.get("content") and not contains_sensitive_data(candidate["content"]):
                self.store.upsert_fact(self.owner_id, candidate.get("category", "note"), candidate.get("key", candidate["content"][:80]), candidate["content"], candidate.get("value"), "sigmai", float(candidate.get("confidence", 0.65)))
        if reason == "event" and payload:
            self.store.mark_event_triggered(payload.get("id"), datetime.fromtimestamp(now).strftime("%Y"))
        followup = result.get("followup") or {}
        if reason == "followup" and payload:
            self.store.finish_followup(payload.get("id"), now + 12 * 3600)
            next_attempt = int(payload.get("attempt", 0)) + 1
            max_attempts = int(payload.get("max_attempts", 4))
            delays = (45 * 60, 90 * 60, 180 * 60)
            if next_attempt < max_attempts and state.get("followup_enabled", True):
                delay = delays[min(next_attempt - 1, len(delays) - 1)]
                self.store.add_followup(
                    self.owner_id, topic, {"seed": result["message_parts"][-1]},
                    now + delay, next_attempt, max_attempts)
        elif followup.get("enabled") and state.get("followup_enabled", True):
            self.store.add_followup(
                self.owner_id, topic, {"seed": result["message_parts"][-1]},
                now + 20 * 60, 0, int(followup.get("max_attempts", 4)))
        return {"topic": topic, "priority": priority, "reason": reason, "message_parts": result["message_parts"]}

    def _build_prompt(self, reason, payload, now, roleplay_hint=""):
        dt = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
        return ("你是最高主人私聊中的长期陪伴 bot。你只能输出 JSON，不要输出 Markdown。\n"
                "你要像一个有连续情绪、记忆和关系状态的人，但不要声称拥有真实意识。\n"
                "当前时间：{}；触发原因：{}；触发数据：{}\n角色扮演状态：{}\n{}\n"
                "请决定本轮是否自然地联系主人。没有充分理由时 should_send=false；普通关怀只写一段且不超过120字，避免追问、套话和重复表达。"
                "你没有真实身体、住所和周边环境；严禁声称自己刚吃饭、洗澡、喝茶、在食堂，或描述自己窗外的天气、阳光、雨声、蝉鸣。"
                "可以关心主人所在地区，但只能说‘如果你那边……’，不能编造‘我这里也……’之类的现实经历。"
                "重要事件可以更热烈，但不要机械重复。严格返回字段：should_send,priority,topic,message_parts,emotion_delta,memory_candidates,followup,media_request。"
                "不要在 message_parts 里泄漏这些内部字段。".format(
                    dt, reason, json.dumps(payload or {}, ensure_ascii=False),
                    roleplay_hint or "未启用活动角色", self.context()))

    @staticmethod
    def _parse_result(raw):
        if not raw:
            return None
        text = str(raw).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I | re.M).strip()
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            if text and not text.startswith("["):
                return {"should_send": True, "topic": "companion", "priority": "normal",
                        "message_parts": [text[:1800]], "emotion_delta": {},
                        "memory_candidates": [], "followup": {"enabled": False},
                        "media_request": {}}
            return None
        if not isinstance(value, dict) or not value.get("should_send", True):
            return None
        parts = value.get("message_parts")
        if isinstance(parts, str):
            parts = [parts]
        if not isinstance(parts, list):
            return None
        value["message_parts"] = [str(item).strip()[:1800] for item in parts[:4] if str(item).strip()]
        return value if value["message_parts"] else None

    def search_memory(self, query=""):
        return {"facts": self.store.list_facts(self.owner_id, query, 50),
                "episodes": self.store.search_episodes(self.owner_id, query, 20),
                "state": self.state()}
