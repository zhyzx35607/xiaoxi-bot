"""Canonical security package with a compatible public module surface."""

import sys as _sys
import types as _types

from .core import *  # noqa: F401,F403
from . import core as _core


def _export_private(owner):
    for name, value in vars(owner).items():
        if name.startswith("_") and not name.startswith("__"):
            globals()[name] = value


_export_private(_core)


class _CompatibilityModule(_types.ModuleType):
    def __setattr__(self, name, value):
        if name not in {"_core", "_types", "_sys"} and hasattr(_core, name):
            setattr(_core, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if hasattr(_core, name):
            try:
                delattr(_core, name)
            except AttributeError:
                pass
        super().__delattr__(name)


_sys.modules[__name__].__class__ = _CompatibilityModule
