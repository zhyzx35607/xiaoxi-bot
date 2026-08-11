"""Atomic JSON state store with bounded, scope-specific records."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable

from ...memory import sanitize_persistent_value
from ...utils import atomic_write_json


class AgentJsonStore:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self._lock = threading.RLock()

    def _path(self, relative):
        path = os.path.abspath(os.path.join(self.root, relative))
        if os.path.commonpath([path, self.root]) != self.root:
            raise ValueError("agent store path escapes root")
        return path

    def _read_unlocked(self, relative, default):
        try:
            with open(self._path(relative), encoding="utf-8") as handle:
                value = json.load(handle)
            sanitized = sanitize_persistent_value(value)
            if sanitized != value:
                self._write_unlocked(relative, sanitized)
            return sanitized
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    def read(self, relative, default):
        with self._lock:
            return self._read_unlocked(relative, default)

    def _write_unlocked(self, relative, value):
        atomic_write_json(
            self._path(relative),
            sanitize_persistent_value(value),
            indent=2,
        )

    def write(self, relative, value):
        with self._lock:
            self._write_unlocked(relative, value)

    def update(self, relative, default, updater: Callable):
        """Apply a read-modify-write operation while holding the store lock."""
        with self._lock:
            current = self._read_unlocked(relative, default)
            updated, result = updater(current)
            self._write_unlocked(relative, updated)
            return result

    def update_many(self, defaults, updater: Callable):
        """Update related JSON documents without allowing concurrent writers."""
        with self._lock:
            current = {
                relative: self._read_unlocked(relative, default)
                for relative, default in defaults.items()
            }
            updated, result = updater(current)
            for relative, value in updated.items():
                self._write_unlocked(relative, value)
            return result

    def append_bounded(self, relative, value, limit=200):
        def append(records):
            if not isinstance(records, list):
                records = []
            records = (records + [value])[-max(1, int(limit)):]
            return records, records

        return self.update(relative, [], append)

    def append_bounded_unique(self, relative, value, *, key, limit=200):
        """Append once for a stable key while preserving bounded storage."""
        def append(records):
            if not isinstance(records, list):
                records = []
            marker = value.get(key) if isinstance(value, dict) else None
            if marker is not None and any(
                    isinstance(item, dict) and item.get(key) == marker
                    for item in records):
                return records, False
            records = (records + [value])[-max(1, int(limit)):]
            return records, True

        return self.update(relative, [], append)
