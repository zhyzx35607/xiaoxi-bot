"""bot/memory.py - Extract user info (name, interests) from conversation.

Does NOT write files directly — ai.py handles persistence alongside its
own user-memory storage, keeping everything in one file per user.
"""

import re
import logging

log = logging.getLogger("qqbot")

_SENSITIVE_PATTERNS = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"), "[证件号]"),
    (re.compile(r"(?i)\b(?:sk|sk-proj|sk-svcacct)-[A-Za-z0-9_.-]{12,}\b"), "[密钥]"),
    (re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{12,}\b"), "[密钥]"),
    (re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{12,}\b"), "[密钥]"),
    (re.compile(r"(?i)\b(?:xox[baprs])-[A-Za-z0-9-]{12,}\b"), "[密钥]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[密钥]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[密钥]"),
    (re.compile(r"(?i)\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[令牌]"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), "[私钥已隐藏]"),
]

_SENSITIVE_MARKER_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:"
    r"密码|口令|password|passwd|passkey|token|令牌|"
    r"api[\s_-]?key|密钥|cookie|authorization|proxy[\s_-]?authorization|"
    r"client[\s_-]?key|csrf|rkey|access[\s_-]?token|sessdata|secret|"
    r"private[\s_-]?key|bearer"
    r")(?![A-Za-z0-9_])"
)
_CREDENTIAL_VALUE_PATTERNS = tuple(pattern for pattern, _ in _SENSITIVE_PATTERNS[2:])
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:password|passwd|passkey|token|令牌|api[\s_-]?key|密钥|"
    r"cookie|set-cookie|authorization|proxy[\s_-]?authorization|client[\s_-]?key|"
    r"csrf|rkey|access[\s_-]?token|sessdata|secret|private[\s_-]?key)"
    r"\s*[:=]\s*(?:bearer\s+)?)([^\s,;]+)"
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|key|secret|sig|signature|auth)="
    r")[^&#\s]+"
)
_SENSITIVE_KEYS = {
    "password", "passwd", "passkey", "token", "access_token", "apikey", "api_key",
    "authorization", "proxy_authorization", "cookie", "cookies", "set_cookie",
    "client_key", "csrf", "rkey", "sessdata", "secret", "private_key", "credential",
    "credentials", "onebot_token",
}


def contains_sensitive_data(text: str) -> bool:
    """Return whether text is unsafe to persist as a memory or event body."""
    original = str(text or "")
    if _SENSITIVE_MARKER_RE.search(original):
        return True
    if any(pattern.search(original) for pattern, _ in _SENSITIVE_PATTERNS[:2]):
        return True
    return any(pattern.search(original) for pattern in _CREDENTIAL_VALUE_PATTERNS)


def redact_sensitive_text(text, *, limit=None):
    """Redact credential-shaped values, optionally applying a caller-owned limit."""
    value = str(text or "")
    value = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1[已隐藏]", value)
    value = _URL_CREDENTIAL_RE.sub(r"\1[已隐藏]", value)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        value = pattern.sub(replacement, value)
    return value[:limit] if limit is not None else value


def sanitize_for_memory(text):
    """Redact sensitive values and cap short-term memory entries."""
    return redact_sensitive_text(text, limit=500)


def sanitize_persistent_value(value, *, depth=0):
    """Recursively redact credential-shaped values before durable writes."""
    if depth > 8:
        return "[嵌套内容已省略]"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = redact_sensitive_text(key)
            normalized = re.sub(r"[^a-z0-9_]+", "_", key_text.lower()).strip("_")
            result[key_text] = (
                "[已隐藏]"
                if normalized in _SENSITIVE_KEYS
                else sanitize_persistent_value(item, depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        return [sanitize_persistent_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [sanitize_persistent_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def extract_user_info(user_msg):
    """Parse a user message for name and interest signals.

    Returns a list of strings like ["称呼: 小明", "喜欢: 打游戏"].
    These should be appended as system entries to the user memory file
    by the caller (ai.py).
    """
    user_msg = str(user_msg or "").strip()
    info = []
    if not user_msg or "?" in user_msg or "？" in user_msg:
        return info

    name_patterns = [
        r"我叫\s*(\S{1,8})",
        r"我是(?:叫|昵称是|大家叫我)\s*(\S{1,8})",
        r"称呼我\s*(\S{1,8})",
        r"喊我\s*(\S{1,8})",
        r"叫我\s*(\S{1,8})",
    ]
    for pat in name_patterns:
        m = re.search(pat, user_msg)
        if m:
            value = sanitize_for_memory(m.group(1)).strip("，。！？,.! ")
            if value and not any(w in value for w in ("不是", "一个", "来自", "做", "在")):
                info.append(f"称呼: {value}")
            break

    interest_patterns = [
        r"我喜欢\s*(.{2,20})",
        r"我爱\s*(.{2,20})",
        r"我.*?喜欢\s*(.{2,20})",
    ]
    for pat in interest_patterns:
        m = re.search(pat, user_msg)
        if m:
            value = sanitize_for_memory(m.group(1)).strip("，。！？,.! ")
            if value and len(value) >= 2:
                info.append(f"喜欢: {value}")
            break

    return info
