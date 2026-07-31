"""Compatibility aggregation for the historical command runtime module."""

from .admin import *  # noqa: F401,F403
from .fun import *  # noqa: F401,F403
from .media import *  # noqa: F401,F403
from .moderation import *  # noqa: F401,F403
from .queries import *  # noqa: F401,F403
from .registry import register_all
from .system import *  # noqa: F401,F403

from . import admin as _admin
from . import common as _common
from . import fun as _fun
from . import media as _media
from . import moderation as _moderation
from . import queries as _queries
from . import registry as _registry
from . import system as _system

_OWNERS = (_admin, _common, _fun, _media, _moderation, _queries, _registry, _system)


def _export_private(owner):
    for name, value in vars(owner).items():
        if name.startswith("_") and not name.startswith("__"):
            globals()[name] = value


for _module_owner in _OWNERS:
    _export_private(_module_owner)
