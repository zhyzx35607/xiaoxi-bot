"""Compatibility proxy for :mod:`bot.integrations.uapi`."""

import sys as _sys
import types as _types

from .integrations import uapi as _implementation


def _export():
    for key, value in vars(_implementation).items():
        if not key.startswith("_"):
            globals()[key] = value


_export()
__all__ = [key for key in vars(_implementation) if not key.startswith("_")]


class _CompatibilityModule(_types.ModuleType):
    def __getattr__(self, key):
        # Private names (e.g. mutable module state such as _state) are not
        # copied into this facade; resolve them live so rebinds in the
        # implementation stay visible through the facade.
        if key.startswith("__"):
            raise AttributeError(key)
        try:
            return getattr(_implementation, key)
        except AttributeError:
            raise AttributeError(
                "module {!r} has no attribute {!r}".format(self.__name__, key)
            ) from None

    def __setattr__(self, key, value):
        # Always mirror into the implementation: patch.object teardown
        # restores originals with del+set, so the target attribute may not
        # exist there yet when the restore write arrives.
        if not key.startswith("__") and key not in {"_implementation", "_types", "_sys"}:
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
