"""AI compatibility facade.

The package keeps the historical ``bot.ai`` import surface while the
implementation is split into focused modules.
"""

import sys as _sys
import types as _types

from .runtime import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
from .memory import *  # noqa: F401,F403
from .providers import *  # noqa: F401,F403
from .stickers import *  # noqa: F401,F403
from .tools import *  # noqa: F401,F403
from .reply import (
    _build_group_reply_segments,
    _parse_reply_actions,
    _parse_reply_tags,
    _prepare_group_reply,
)
from . import runtime as _runtime
from . import prompts as _prompts
from . import search as _search
from . import memory as _memory
from . import providers as _providers
from . import stickers as _stickers
from . import tools as _tools

# Preserve private helpers imported by existing tests and integrations.
_OWNERS = (_runtime, _prompts, _search, _memory, _providers, _stickers, _tools)
for _owner in _OWNERS:
    for _name in dir(_owner):
        if _name.startswith("_") and not _name.startswith("__"):
            globals()[_name] = getattr(_owner, _name)

__all__ = [name for name in globals() if not name.startswith("_")]


class _CompatibilityModule(_types.ModuleType):
    """Forward monkeypatches of legacy bot.ai attributes to runtime."""

    def __setattr__(self, name, value):
        if name not in {
            "_runtime", "_prompts", "_search", "_memory", "_providers",
            "_stickers", "_tools", "_types", "_sys", "_OWNERS",
        }:
            for owner in _OWNERS:
                if hasattr(owner, name):
                    setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        for owner in _OWNERS:
            if hasattr(owner, name):
                try:
                    delattr(owner, name)
                except AttributeError:
                    pass
        super().__delattr__(name)


_sys.modules[__name__].__class__ = _CompatibilityModule
