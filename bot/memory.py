"""bot/memory.py - Extract user info (name, interests) from conversation.

Does NOT write files directly — ai.py handles persistence alongside its
own user-memory storage, keeping everything in one file per user.
"""

import re
import logging

log = logging.getLogger("qqbot")

_SENSITIVE_PATTERNS = [
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '[手机号]'),
    (re.compile(r'(?<!\d)\d{17}[0-9Xx](?!\d)'), '[证件号]'),
    (re.compile(r'(?i)(?:sk-|api[_-]?key\s*[:=]\s*)[A-Za-z0-9_.-]{12,}'), '[密钥]'),
    (re.compile(r'(?i)(密码|口令|password)\s*[:：=]?\s*\S{4,}'), r'\1：[已隐藏]'),
]


def sanitize_for_memory(text):
    """Redact sensitive values before persistent memory writes."""
    value = str(text or "")
    for pattern, replacement in _SENSITIVE_PATTERNS:
        value = pattern.sub(replacement, value)
    return value[:500]


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
