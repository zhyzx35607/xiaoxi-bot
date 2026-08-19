"""AI reply parsing and OneBot message segment helpers."""

import re

_LINE_COMMAND_PREFIX = re.compile(r"^/(?=[^\s/])")


def strip_command_prefix(text):
    """Rewrite a leading "/" on every line of AI output to fullwidth "／".

    Bot messages echo back through message_sent, where a line starting with
    "/" would run as an owner-level command; AI text never legitimately
    starts a line with a command, so neutralize it at the output side.
    """
    if not text:
        return text
    return "\n".join(
        _LINE_COMMAND_PREFIX.sub("／", line) for line in str(text).split("\n"))

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
    
    # Backward compatibility for old prompts that emitted plain @nickname.
    # Match known nicknames exactly instead of consuming adjacent message text.
    nicknames = sorted((str(name) for name in member_map if name),
                       key=len, reverse=True)
    if nicknames:
        nickname_pattern = '|'.join(_re.escape(nick) for nick in nicknames)
        at_pattern = _re.compile(
            r'@(' + nickname_pattern + r')(?=$|[\s，。！？、,.!?:：；;）)\]}])')

        def replace_known_mention(match):
            nick = match.group(1)
            at_qqs.append(member_map[nick])
            return ''

        reply = at_pattern.sub(replace_known_mention, reply)
    # Never emit a fake textual mention when a natural @ target is ambiguous.
    reply = _re.sub(r'(?<!\S)@(?=\S)', '', reply)
    
    # Clean up extra whitespace
    reply = _re.sub(r'\s+', ' ', reply).strip()
    
    return reply, at_qqs, quote_text

def _build_group_reply_segments(text, at_qqs=()):
    """Build OneBot segments with visible spacing after real mentions."""
    segments = []
    for qq in list(at_qqs or ())[:2]:
        segments.append({"type": "at", "data": {"qq": str(qq)}})
        segments.append({"type": "text", "data": {"text": " "}})
    if text:
        segments.append({"type": "text", "data": {"text": str(text)}})
    return segments

def _prepare_group_reply(reply, member_map, *, user_id=0, message_id=0):
    """Resolve AI reply actions into text and safe OneBot targets."""
    clean_reply, tagged_actions = _parse_reply_tags(reply, member_map)
    at_qqs = [int(action["qq"]) for action in tagged_actions
              if action.get("type") == "at"
              and str(action.get("qq", "")).isdigit()]
    wants_reply = any(action.get("type") == "reply"
                      for action in tagged_actions)
    poke_targets = [action.get("target") for action in tagged_actions
                    if action.get("type") == "poke"]
    clean_reply, legacy_at, quote_text = _parse_reply_actions(
        clean_reply, member_map)
    at_qqs.extend(legacy_at)
    at_qqs = list(dict.fromkeys(at_qqs))[:2]
    if wants_reply and message_id:
        quote_text = quote_text or "reply"
    if quote_text and message_id and user_id:
        at_qqs = [qq for qq in at_qqs if str(qq) != str(user_id)]
    return clean_reply, at_qqs, quote_text, poke_targets

def _parse_reply_tags(reply, member_map):
    """Parse feature tags from AI reply text.
    member_map: dict of {nickname: qq_number} for @ resolution.
    Returns:
        clean_reply (str): reply with tags stripped
        actions (list): list of action dicts to execute
    """
    import re as _re_tag
    actions = []
    # 1. [STICKER:emotion] — already handled elsewhere
    reply = _re_tag.sub(r'\[STICKER:[^\]]+\]', '', reply)
    # 2. [POKE:nickname]
    _poke_match = _re_tag.search(r'\[POKE:([^\]]+)\]', reply)
    if _poke_match:
        nick = _poke_match.group(1).strip()
        qq = member_map.get(nick, 0)
        if qq:
            actions.append({"type": "poke", "target": qq})
        reply = reply.replace(_poke_match.group(0), '').strip()
    # 3. [AT:nickname] — resolve nickname to QQ
    while True:
        _at_match = _re_tag.search(r'\[AT:([^\]]+)\]', reply)
        if not _at_match:
            break
        nick = _at_match.group(1).strip()
        qq = member_map.get(nick, 0)
        if qq:
            actions.append({"type": "at", "qq": str(qq)})
        replacement = '' if qq else nick
        reply = reply.replace(_at_match.group(0), replacement, 1)
    # 4. [REPLY] — flag to reply to the original message
    if '[REPLY]' in reply:
        actions.append({"type": "reply"})
        reply = reply.replace('[REPLY]', '').strip()
    # 5. Clean up whitespace
    reply = _re_tag.sub(r'\s+', ' ', reply).strip()
    return reply, actions
