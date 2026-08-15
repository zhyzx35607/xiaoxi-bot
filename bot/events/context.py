"""Shared event scope, logging, card, and service helpers."""

import asyncio
import logging
import os

from ..permission import get_group_config, is_group_enabled

chat_log = logging.getLogger("qqbot.chat")

def _private_chat_allowed(dispatcher, user_id):
    """Return whether a private-message sender may enter any bot pipeline."""
    pc_cfg = dispatcher.config.get("private_chat", {})
    if user_id == dispatcher.config.get("bot_owner"):
        return True
    allowed_users = {
        int(value) for value in pc_cfg.get("allowed_users", [])
        if str(value).isdigit()
    }
    return bool(pc_cfg.get("enabled", False) or user_id in allowed_users)

def _disabled_group_activation_allowed(dispatcher, event):
    """Allow only the owner/bot account/group masters to recover a disabled group."""
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return False
    user_id = event.get("user_id")
    if user_id == dispatcher.config.get("bot_owner"):
        return True
    prefix = dispatcher.config.get("command_prefix", "/")
    is_enable = str(event.get("raw_message") or "").strip().lower() == prefix + "enable"
    if user_id == dispatcher.config.get("bot_qq"):
        return is_enable
    # Group masters may re-enable their own group, but only via /enable —
    # everything else from a disabled group stays gated out.
    if is_enable:
        masters = get_group_config(dispatcher, event.get("group_id")).get("masters", [])
        return any(str(master) == str(user_id) for master in masters)
    return False

def _event_scope_allowed(dispatcher, event):
    """Hard scope gate applied before parsing, logging, caching, or AI work."""
    group_id = event.get("group_id")
    if group_id and not is_group_enabled(dispatcher, group_id):
        return _disabled_group_activation_allowed(dispatcher, event)
    if (event.get("post_type") in ("message", "message_sent")
            and event.get("message_type") == "private"):
        # message_sent is the bot's own echo: user_id is the bot account,
        # target_id is the actual chat peer.
        peer_id = event.get("user_id", 0)
        if event.get("post_type") == "message_sent":
            peer_id = event.get("target_id") or peer_id
        return _private_chat_allowed(dispatcher, peer_id)
    return True

def _log_chat_message(dispatcher, direction, raw, group_id=None, user_id=0, sender_name=""):
    """Write bounded chat history only for explicitly permitted scopes."""
    if group_id and not is_group_enabled(dispatcher, group_id):
        return False
    if not group_id and not _private_chat_allowed(dispatcher, user_id):
        return False
    text = str(raw or "").replace("\r", "\\r").replace("\n", "\\n")[:500]
    if group_id:
        chat_log.info("%s group=%s user=%s name=%s text=%s",
                      direction, group_id, user_id, sender_name, text)
    else:
        chat_log.info("%s user=%s name=%s text=%s",
                      direction, user_id, sender_name, text)
    return True

def _cq_unescape(text):
    """Undo CQ-code entity escaping (&#91; &#93; &#44; &amp;).

    NapCat puts share cards inline into raw_message as [CQ:json,data=...],
    where the JSON payload is entity-escaped and URLs use \\/ sequences."""
    return (text.replace("&#91;", "[").replace("&#93;", "]")
            .replace("&#44;", ",").replace("&amp;", "&"))

def _share_card_text(message):
    """Pull searchable text out of QQ share-card (json) segments.

    NapCat leaves raw_message empty for pure card messages; without this the
    dispatcher never sees Bilibili links shared as cards."""
    texts = []
    for seg in message or []:
        if not isinstance(seg, dict) or seg.get("type") != "json":
            continue
        data = seg.get("data") or {}
        payload = data.get("data")
        if isinstance(payload, str):
            texts.append(payload.replace("\\/", "/"))
    return "\n".join(texts)

def _read_tail_text(path, line_count=30, max_bytes=65536, max_chars=4000):
    """Read a small tail window without loading the whole rotating log."""
    line_count = max(1, min(int(line_count), 200))
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks = []
            total = 0
            newline_count = 0
            while position > 0 and total < max_bytes and newline_count <= line_count:
                size = min(4096, position, max_bytes - total)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                total += len(chunk)
                newline_count += chunk.count(b"\n")
        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-line_count:])[-max_chars:]
    except FileNotFoundError:
        return ""

async def _service_state(service_name):
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "is-active", service_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return "timeout"
    return stdout.decode("utf-8", errors="replace").strip() or "unknown"
