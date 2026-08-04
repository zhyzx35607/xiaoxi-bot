"""Scoped long-term goals for owner and group Agent workspaces."""

import time
import uuid


class AgentGoalStore:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return "goals/{}.json".format(scope_key.replace(":", "_").replace("/", "_"))

    def list(self, scope_key, *, include_done=False):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return []
        if include_done:
            return records
        return [item for item in records if item.get("status") not in {"done", "cancelled"}]

    def create(self, scope_key, owner_id, title):
        now = time.time()
        goal = {
            "id": uuid.uuid4().hex[:12],
            "scope_key": scope_key,
            "owner_id": int(owner_id),
            "title": str(title).strip()[:500],
            "status": "active",
            "progress": "",
            "created_at": now,
            "updated_at": now,
        }
        records = self.list(scope_key, include_done=True)
        records.append(goal)
        self.store.write(self._path(scope_key), records[-200:])
        return goal

    def update(self, scope_key, goal_id, *, status=None, progress=None):
        records = self.list(scope_key, include_done=True)
        for item in records:
            if item.get("id") != str(goal_id):
                continue
            if status:
                item["status"] = str(status)[:30]
            if progress is not None:
                item["progress"] = str(progress)[:1000]
            item["updated_at"] = time.time()
            self.store.write(self._path(scope_key), records[-200:])
            return item
        return None
