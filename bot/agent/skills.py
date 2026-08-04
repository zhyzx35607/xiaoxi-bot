"""Reusable scope-isolated Agent SOPs."""

import time
import uuid


class AgentSkillStore:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return "skills/{}.json".format(scope_key.replace(":", "_"))

    def create(self, scope_key, owner_id, name, instructions, *, triggers=None):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            records = []
        now = time.time()
        item = {
            "id": uuid.uuid4().hex[:12], "name": str(name).strip()[:100],
            "instructions": str(instructions).strip()[:4000],
            "triggers": [str(value).strip()[:80] for value in (triggers or []) if str(value).strip()][:20],
            "owner_id": int(owner_id or 0), "enabled": True,
            "created_at": now, "updated_at": now,
        }
        records.append(item)
        self.store.write(self._path(scope_key), records[-100:])
        return item

    def list(self, scope_key, enabled_only=True):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return []
        if enabled_only:
            records = [item for item in records if item.get("enabled", True)]
        return sorted(records, key=lambda item: item.get("updated_at", 0), reverse=True)

    def match(self, scope_key, text, limit=5):
        lowered = str(text).lower()
        matches = []
        for item in self.list(scope_key):
            triggers = item.get("triggers") or []
            if not triggers or any(trigger.lower() in lowered for trigger in triggers):
                matches.append(item)
        return matches[:limit]

    def set_enabled(self, scope_key, skill_id, enabled):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return None
        updated = None
        for item in records:
            if item.get("id") == skill_id:
                item["enabled"] = bool(enabled)
                item["updated_at"] = time.time()
                updated = item
                break
        if updated:
            self.store.write(self._path(scope_key), records[-100:])
        return updated
