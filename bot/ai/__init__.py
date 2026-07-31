"""AI compatibility facade.

The package keeps the historical ``bot.ai`` import surface while the
implementation is split into focused modules.
"""

import sys as _sys
import types as _types

from .runtime import *  # noqa: F401,F403
from .reply import (
    _build_group_reply_segments,
    _parse_reply_actions,
    _parse_reply_tags,
    _prepare_group_reply,
)
from . import runtime as _runtime

# Preserve private helpers imported by existing tests and integrations.
for _name in dir(_runtime):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_runtime, _name)

__all__ = [name for name in globals() if not name.startswith("_")]


class _CompatibilityModule(_types.ModuleType):
    """Forward monkeypatches of legacy bot.ai attributes to runtime."""

    def __setattr__(self, name, value):
        if name not in {"_runtime", "_types", "_sys"} and hasattr(_runtime, name):
            setattr(_runtime, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if hasattr(_runtime, name):
            try:
                delattr(_runtime, name)
            except AttributeError:
                pass
        super().__delattr__(name)


_sys.modules[__name__].__class__ = _CompatibilityModule
