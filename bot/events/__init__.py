"""Normalized event handling boundaries."""

from .notice import handle_notice
from .request import handle_request

__all__ = ["handle_notice", "handle_request"]
