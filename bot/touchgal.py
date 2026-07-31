"""Compatibility proxy for :mod:`bot.integrations.touchgal`."""

import sys as _sys
import types as _types

from .integrations import touchgal as _implementation


def _export():
    for key, value in vars(_implementation).items():
        if not key.startswith("__"):
            globals()[key] = value


_export()
__all__ = [key for key in vars(_implementation) if not key.startswith("_")]


class _CompatibilityModule(_types.ModuleType):
    def __setattr__(self, key, value):
        if key not in {"_implementation", "_types", "_sys"} and hasattr(_implementation, key):
            setattr(_implementation, key, value)
        super().__setattr__(key, value)

    def __delattr__(self, key):
        if hasattr(_implementation, key):
            try:
                delattr(_implementation, key)
            except AttributeError:
                pass
        super().__delattr__(key)


_sys.modules[__name__].__class__ = _CompatibilityModule
