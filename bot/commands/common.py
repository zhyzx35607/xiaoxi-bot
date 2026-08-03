"""Shared command configuration persistence helpers."""

import json
import os

from ..utils import atomic_write_json

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.getenv("QQBOT_CONFIG_PATH") or os.path.join(_ROOT, "config.json")

def _load():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def _save(c):
    atomic_write_json(CONFIG_PATH, c, indent=2)
