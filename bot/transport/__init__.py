"""OneBot transport and message action primitives."""

from .onebot import OneBotClient
from .segments import at_segment, image_segment, reply_segment, text_segment

__all__ = [
    "OneBotClient",
    "at_segment",
    "image_segment",
    "reply_segment",
    "text_segment",
]
