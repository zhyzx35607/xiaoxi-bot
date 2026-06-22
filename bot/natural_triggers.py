"""bot/natural_triggers.py - Natural language command triggers (no / prefix needed)"""

import re

# ==================== TRIGGER DEFINITIONS ====================

# Pattern-based triggers: must contain @mention + keyword
# All keywords use word-boundary matching to prevent substring false positives
# Format: (command_name, keyword_list, requires_at)
PATTERN_TRIGGERS = [
    ("kick", ["踢", "踹", "踢出", "送走", "叉出去", "出去"], True),
    ("ban", ["禁", "禁言", "ban", "闭嘴", "闭嘴吧", "关小黑屋", "塞口球", "静音", "别说了", "消停会"], True),
    ("unban", ["解", "解禁", "放出来", "解除禁言", "unban", "张嘴", "原谅你了", "放出来吧"], True),
]

# Exact phrase triggers: must match exactly (case-insensitive)
PHRASE_TRIGGERS = [
    ("like", [
        "赞我", "给我点赞", "点个赞", "点点赞", "点个赞呗",
        "给个赞", "来个赞", "点个赞吧", "求点赞",
    ]),
    ("fortune", [
        "今日运势", "抽个签", "求签", "运势如何",
        "今天运势", "抽签", "测运势", "运气怎么样",
    ]),
    ("rank", [
        "发言排行", "水群排行", "谁最能水", "群排行",
        "看看排名", "话痨榜", "谁话最多", "水群排名",
    ]),
    ("精华", [
        "设精华", "设为精华", "加精华",
    ]),
]

# Music trigger prefixes: message starts with one of these
MUSIC_PREFIXES = [
    "点歌", "我要点歌", "点首", "来首", "放首",
    "搜歌", "帮我点歌", "我想点歌", "点一下歌",
]


def _keyword_boundary_pattern(kw):
    """Build a regex that matches `kw` at word boundaries.
    
    A word boundary means: preceded by start-of-string, space, or CJK punctuation,
    and followed by space, CJK punctuation, @, or end-of-string.
    This prevents false matches like "飞" in "飞八分钱" or "出去" in "发出去".
    """
    boundary = r"(?:^|[\s,，。！？!])"
    end_boundary = r"(?:[\s,，。！？!@]|$)"
    return boundary + re.escape(kw) + end_boundary


def check_natural_triggers(raw_message, message_segments):
    """Check if a message matches any natural language trigger.
    
    Returns (command_name, args_dict) or None.
    """
    if not raw_message or len(raw_message.strip()) < 2:
        return None
    
    # Strip CQ codes to get text-only content
    text_only = re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
    
    # Extract @mentions from message segments
    at_targets = []
    if message_segments:
        for seg in message_segments:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = seg.get("data", {}).get("qq", "")
                if qq and qq != "all":
                    at_targets.append(int(qq))
    
    # ---- Pattern triggers (require @, word-boundary matching) ----
    if at_targets:
        for cmd_name, keywords, _ in PATTERN_TRIGGERS:
            for kw in keywords:
                pattern = _keyword_boundary_pattern(kw)
                if re.search(pattern, text_only):
                    # Extract args after the keyword
                    args_parts = text_only.split(kw, 1)[-1].strip()
                    args_parts = re.sub(r"\[CQ:[^\]]+\]", "", args_parts).strip()
                    args_val = ""
                    if cmd_name == "ban":
                        num_match = re.search(r"(\d+)", args_parts)
                        if num_match:
                            args_val = num_match.group(1)
                    return (cmd_name, {"targets": at_targets, "args": args_val})
    
    # ---- Phrase triggers ----
    for cmd_name, phrases in PHRASE_TRIGGERS:
        for phrase in phrases:
            if text_only == phrase:
                return (cmd_name, {})
    
    # ---- Music prefixes ----
    for prefix in MUSIC_PREFIXES:
        if text_only.startswith(prefix):
            keyword = text_only[len(prefix):].strip()
            if keyword:
                return ("music", {"keyword": keyword})
    
    return None


def is_music_trigger(raw_message):
    """Quick check if message starts with a music prefix."""
    if not raw_message:
        return False, ""
    text_only = re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
    for prefix in MUSIC_PREFIXES:
        if text_only.startswith(prefix):
            keyword = text_only[len(prefix):].strip()
            if keyword:
                return True, keyword
    return False, ""