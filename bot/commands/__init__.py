"""Command compatibility facade and public registration entrypoint."""

import sys as _sys
import types as _types

from .runtime import *  # noqa: F401,F403
from . import runtime as _runtime

for _name in dir(_runtime):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_runtime, _name)


class _CompatibilityModule(_types.ModuleType):
    """Forward legacy monkeypatches to the command runtime module."""

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
