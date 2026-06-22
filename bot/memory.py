"""bot/memory.py - Extract user info (name, interests) from conversation.

Does NOT write files directly — ai.py handles persistence alongside its
own user-memory storage, keeping everything in one file per user.
"""

import re
import logging

log = logging.getLogger("qqbot")


def extract_user_info(user_msg):
    """Parse a user message for name and interest signals.

    Returns a list of strings like ["称呼: 小明", "喜欢: 打游戏"].
    These should be appended as system entries to the user memory file
    by the caller (ai.py).
    """
    info = []

    name_patterns = [
        r"我叫\s*(\S{1,8})",
        r"我是\s*(\S{1,8})",
        r"称呼我\s*(\S{1,8})",
        r"喊我\s*(\S{1,8})",
        r"叫我\s*(\S{1,8})",
    ]
    for pat in name_patterns:
        m = re.search(pat, user_msg)
        if m:
            info.append(f"称呼: {m.group(1)}")
            break

    interest_patterns = [
        r"我喜欢\s*(.{2,20})",
        r"我爱\s*(.{2,20})",
        r"我.*?喜欢\s*(.{2,20})",
    ]
    for pat in interest_patterns:
        m = re.search(pat, user_msg)
        if m:
            info.append(f"喜欢: {m.group(1)}")
            break

    return info
