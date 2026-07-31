"""Compatibility wrapper for transport message actions."""

from bot.transport.actions import (
    at_segment,
    execute_message_action,
    reply_segment,
    text_segment,
)

__all__ = [
    "at_segment",
    "execute_message_action",
    "reply_segment",
    "text_segment",
]
