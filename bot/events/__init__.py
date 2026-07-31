"""Normalized event handling boundaries."""

__all__ = [
    "GroupMessageMixin",
    "PrivateMessageMixin",
    "RouterMixin",
    "handle_notice",
    "handle_request",
]


def __getattr__(name):
    if name in {"GroupMessageMixin", "PrivateMessageMixin"}:
        from . import message
        return getattr(message, name)
    if name == "RouterMixin":
        from .router import RouterMixin
        return RouterMixin
    if name == "handle_notice":
        from .notice import handle_notice
        return handle_notice
    if name == "handle_request":
        from .request import handle_request
        return handle_request
    raise AttributeError(name)
