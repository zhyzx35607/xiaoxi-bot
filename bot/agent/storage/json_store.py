"""Atomic JSON state store with bounded, scope-specific records."""

import json, os, threading
from ...utils import atomic_write_json

class AgentJsonStore:
    def __init__(self, root): self.root, self._lock = os.path.abspath(root), threading.RLock()
    def _path(self, relative):
        path = os.path.abspath(os.path.join(self.root, relative))
        if os.path.commonpath([path, self.root]) != self.root: raise ValueError("agent store path escapes root")
        return path
    def read(self, relative, default):
        try:
            with open(self._path(relative), encoding="utf-8") as handle: return json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError): return default
    def write(self, relative, value):
        with self._lock: atomic_write_json(self._path(relative), value, indent=2)
    def append_bounded(self, relative, value, limit=200):
        with self._lock:
            records = self.read(relative, [])
            if not isinstance(records, list): records = []
            records.append(value); records = records[-limit:]; self.write(relative, records); return records
