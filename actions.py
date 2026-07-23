"""Validated message actions built on OneBot/NapCat primitives."""

import logging

log = logging.getLogger("qqbot")


def text_segment(text):
    return {"type": "text", "data": {"text": str(text)}}


def at_segment(user_id):
    return {"type": "at", "data": {"qq": str(user_id)}}


def reply_segment(message_id):
    return {"type": "reply", "data": {"id": str(message_id)}}


async def execute_message_action(client, *, group_id=None, user_id=None, text="",
                                reply_to=None, mentions=(), reaction=None,
                                poke_user=None, extra_segments=None):
    """Execute one bounded message action with graceful degradation."""
    if group_id is None and mentions:
        mentions = ()
    mentions = list(mentions or [])[:2]
    segments = []
    if reply_to:
        segments.append(reply_segment(reply_to))
    segments.extend(at_segment(uid) for uid in mentions if uid)
    if text:
        segments.append(text_segment(str(text)[:500]))
    if extra_segments:
        segments.extend(list(extra_segments)[:1])
    if not segments:
        return {"status": "failed", "msg": "empty message action"}

    if group_id:
        result = await client.send_group_msg(group_id, segments)
    elif user_id:
        result = await client.send_private_msg(user_id, segments)
    else:
        return {"status": "failed", "msg": "missing target"}

    if reaction and reply_to:
        try:
            await client.set_msg_emoji_like(reply_to, str(reaction))
        except Exception as exc:
            log.debug("reaction action failed: %s", exc)
    if poke_user:
        try:
            if group_id and hasattr(client, "group_poke"):
                await client.group_poke(group_id, int(poke_user))
            elif user_id and hasattr(client, "friend_poke"):
                await client.friend_poke(int(poke_user))
        except Exception as exc:
            log.debug("poke action failed: %s", exc)
    return result
