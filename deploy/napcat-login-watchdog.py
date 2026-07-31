"""Deployment compatibility entrypoint for the canonical NapCat watchdog."""

import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bot.integrations.napcat import watchdog as _implementation


def _export():
    for name, value in vars(_implementation).items():
        if not name.startswith("__"):
            globals()[name] = value


_export()


class _CompatibilityModule(types.ModuleType):
    def __setattr__(self, name, value):
        if name not in {"_implementation", "types", "sys"} and hasattr(_implementation, name):
            setattr(_implementation, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if hasattr(_implementation, name):
            try:
                delattr(_implementation, name)
            except AttributeError:
                pass
        super().__delattr__(name)


sys.modules[__name__].__class__ = _CompatibilityModule


if __name__ == "__main__":
    _implementation.main()
