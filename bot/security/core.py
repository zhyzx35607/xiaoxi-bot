"""Security helpers for URL checks and gray-tip audit logs."""
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit

from app.logging_setup import sanitize_log_message
from ..permission import LEVEL_ADMIN, _resolve_user_level, get_bot_role
from ..utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_PATH = os.path.join(_ROOT, "data", "security_events.json")
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+", re.IGNORECASE)


def _global_security_config(dispatcher):
    cfg = dispatcher.config.setdefault("security", {})
    cfg.setdefault("url_check_enabled", True)
    cfg.setdefault("gray_tip_protect_enabled", True)
    cfg.setdefault("auto_punish", True)
    cfg.setdefault("ban_seconds", 600)
    cfg.setdefault("max_log_entries", 200)
    return cfg


def security_config(dispatcher, group_id=None):
    cfg = dict(_global_security_config(dispatcher))
    if group_id:
        group_cfg = dispatcher.config.get("groups", {}).get(str(group_id), {})
        cfg.update(group_cfg.get("security", {}) if isinstance(group_cfg.get("security"), dict) else {})
    return cfg


def extract_urls(text):
    urls = []
    seen = set()
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,!?;:，。！？；：")
        key = url.lower()
        if key not in seen:
            seen.add(key)
            urls.append(url)
    return urls[:5]


def load_security_events():
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_security_events(events):
    atomic_write_json(_LOG_PATH, events, indent=2)


def _sanitize_security_detail(detail, limit=500):
    """Keep audit details useful without retaining URL credentials or payloads."""
    text = str(detail or "")

    def strip_url(match):
        raw_url = match.group(0)
        try:
            parsed = urlsplit(raw_url)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            return "<url>"

    text = _URL_RE.sub(strip_url, text)
    return sanitize_log_message(text, limit=limit)


def _gray_tip_metadata(event):
    """Extract stable identifiers only; never persist the full OneBot event."""
    allowed = (
        "notice_type", "sub_type", "group_id", "user_id", "sender_id",
        "operator_id", "message_id", "target_id", "time",
    )
    return {key: event[key] for key in allowed if key in event}


def record_security_event(dispatcher, event_type, group_id, user_id, detail, action="logged"):
    cfg = _global_security_config(dispatcher)
    events = load_security_events()
    events.append({
        "ts": time.time(),
        "type": event_type,
        "group_id": int(group_id or 0),
        "user_id": int(user_id or 0),
        "detail": _sanitize_security_detail(detail),
        "action": action,
    })
    max_entries = int(cfg.get("max_log_entries", 200))
    if len(events) > max_entries:
        events = events[-max_entries:]
    save_security_events(events)


def format_security_events(group_id=None, limit=10):
    events = load_security_events()
    if group_id:
        events = [e for e in events if str(e.get("group_id")) == str(group_id)]
    events = events[-max(1, min(int(limit or 10), 30)):]
    if not events:
        return "没有安全日志"
    lines = ["安全日志"]
    for entry in reversed(events):
        ts = time.strftime("%m-%d %H:%M", time.localtime(entry.get("ts", 0)))
        lines.append(
            "{} [{}] g{} u{} {} {}".format(
                ts,
                entry.get("type", "?"),
                entry.get("group_id", ""),
                entry.get("user_id", ""),
                entry.get("action", "logged"),
                str(entry.get("detail", ""))[:80].replace("\n", " "),
            )
        )
    return "\n".join(lines)


def _flatten_status_values(value):
    values = []
    if isinstance(value, dict):
        for key, val in value.items():
            values.append(str(key))
            values.extend(_flatten_status_values(val))
    elif isinstance(value, list):
        for item in value:
            values.extend(_flatten_status_values(item))
    elif value is not None:
        values.append(str(value))
    return values


def is_url_check_risky(result):
    if not isinstance(result, dict):
        return None, "invalid_response"
    if result.get("status") not in ("ok", "async"):
        return None, "api_not_ok"
    data = result.get("data", result)
    if isinstance(data, dict) and "level" in data:
        try:
            level = int(data.get("level"))
        except (TypeError, ValueError):
            return None, "invalid_level"
        if level >= 3:
            return True, "level={}".format(level)
        if level >= 0:
            return False, "level={}".format(level)
        return None, "invalid_level"
    text = " ".join(_flatten_status_values(data)).lower()
    risky_words = (
        "unsafe", "danger", "dangerous", "malicious", "phishing", "fraud",
        "risk", "risky", "blocked", "black", "illegal", "virus", "trojan",
        "恶意", "危险", "风险", "钓鱼", "诈骗", "欺诈", "木马", "病毒",
        "违法", "违规", "拦截", "黑名单", "不安全",
    )
    safe_words = ("safe", "normal", "white", "安全", "正常", "白名单")
    if any(word in text for word in risky_words):
        return True, text[:160]
    if any(word in text for word in safe_words):
        return False, text[:160]
    return None, text[:160] or "unknown"


async def _can_punish(dispatcher, group_id, user_id, sender_role):
    owner = dispatcher.config.get("bot_owner")
    bot_qq = dispatcher.config.get("bot_qq")
    if user_id in (owner, bot_qq):
        return False, "protected_user"
    user_level, _, verified = await _resolve_user_level(
        dispatcher, group_id, user_id, sender_role)
    if not verified:
        return False, "user_role_unverified"
    if user_level >= LEVEL_ADMIN:
        return False, "sender_is_admin"
    bot_role, _ = await get_bot_role(dispatcher, group_id)
    if bot_role not in ("admin", "owner"):
        return False, "bot_not_admin"
    return True, "ok"


async def punish_security_violation(dispatcher, group_id, user_id, message_id, reason, sender_role="member"):
    cfg = security_config(dispatcher, group_id)
    if not cfg.get("auto_punish", True):
        return "logged"
    can_punish, why = await _can_punish(dispatcher, group_id, user_id, sender_role)
    if not can_punish:
        return "ignored:" + why
    actions = []
    if message_id:
        deleted = await dispatcher.client.delete_msg(message_id)
        actions.append("deleted" if deleted.get("status") == "ok" else "delete_failed")
    duration = int(cfg.get("ban_seconds", 600))
    if duration > 0:
        banned = await dispatcher.client.set_group_ban(group_id, user_id, duration)
        actions.append("banned:{}s".format(duration) if banned.get("status") == "ok" else "ban_failed")
    log.warning("Security action applied: user=%s group=%s", user_id, group_id)
    return ",".join(actions) or "logged"


async def check_message_urls(dispatcher, group_id, user_id, raw, message_id, sender_role="member"):
    cfg = security_config(dispatcher, group_id)
    if not cfg.get("url_check_enabled", True):
        return False
    urls = extract_urls(raw)
    if not urls:
        return False
    for url in urls:
        try:
            result = await dispatcher.client.check_url_safely(url)
        except Exception as e:
            log.warning("URL safety check failed: %s", e)
            reason = "api_error"
            record_security_event(
                dispatcher, "url_check_failed", group_id, user_id,
                url + " | " + reason, "message_ignored",
            )
            return True
        risky, reason = is_url_check_risky(result)
        if risky is None:
            record_security_event(
                dispatcher, "url_check_failed", group_id, user_id,
                url + " | " + reason, "message_ignored",
            )
            return True
        if not risky:
            continue
        action = await punish_security_violation(
            dispatcher, group_id, user_id, message_id,
            "malicious_url " + url + " " + reason,
            sender_role=sender_role,
        )
        record_security_event(dispatcher, "url", group_id, user_id, url + " | " + reason, action)
        return "deleted" in action
    return False


async def handle_gray_tip(dispatcher, event):
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id") or event.get("sender_id") or event.get("operator_id") or 0
    message_id = event.get("message_id", 0)
    metadata = _gray_tip_metadata(event)
    log.warning("Gray tip event logged: group=%s user=%s msg_id=%s", group_id, user_id, message_id)
    record_security_event(dispatcher, "gray_tip", group_id, user_id, metadata, "logged")
