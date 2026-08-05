"""Command compatibility facade and public registration entrypoint."""

import sys as _sys
import types as _types

from .runtime import *  # noqa: F401,F403
from . import runtime as _runtime
from . import admin as _admin
from . import common as _common
from . import fun as _fun
from . import media as _media
from . import moderation as _moderation
from . import queries as _queries
from . import roleplay as _roleplay
from . import registry as _registry
from . import system as _system

_OWNERS = (_runtime, _admin, _common, _fun, _media, _moderation, _queries, _roleplay, _registry, _system)


def _export_private(owner):
    for name, value in vars(owner).items():
        if name.startswith("_") and not name.startswith("__"):
            globals()[name] = value


for _module_owner in _OWNERS:
    _export_private(_module_owner)


class _CompatibilityModule(_types.ModuleType):
    """Forward legacy monkeypatches to the command runtime module."""

    def __setattr__(self, name, value):
        if name not in {
            "_runtime", "_admin", "_common", "_fun", "_media", "_moderation",
            "_queries", "_roleplay", "_registry", "_system", "_types", "_sys", "_OWNERS",
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
