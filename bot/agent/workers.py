"""Background task records for bounded autonomous owner work."""

import time
import uuid


class AgentTaskStore:
    def __init__(self, store):
        self.store = store

    def _records(self):
        records = self.store.read("tasks/index.json", [])
        return records if isinstance(records, list) else []

    def create(self, scope_key, owner_id, goal, *, success_criteria="", status="queued", plan_id=""):
        task = {
            "id": uuid.uuid4().hex[:16],
            "scope_key": scope_key,
            "owner_id": int(owner_id),
            "goal": str(goal)[:1000],
            "success_criteria": str(success_criteria)[:1000],
            "plan_id": str(plan_id)[:20],
            "status": status,
            "attempts": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        records = self._records()
        records.append(task)
        self.store.write("tasks/index.json", records[-500:])
        return task

    def list(self, scope_key=None, statuses=None):
        records = self._records()
        if scope_key:
            records = [item for item in records if item.get("scope_key") == scope_key]
        if statuses:
            statuses = set(statuses)
            records = [item for item in records if item.get("status") in statuses]
        return records

    def next_queued(self, stale_after_seconds=3600):
        records = self._records()
        now = time.time()
        changed = False
        try:
            stale_after_seconds = max(60, int(stale_after_seconds))
        except (TypeError, ValueError):
            stale_after_seconds = 3600
        for item in records:
            if item.get("status") != "running":
                continue
            updated_at = float(item.get("updated_at", 0) or 0)
            if updated_at and now - updated_at >= stale_after_seconds:
                item["status"] = "queued"
                item["error"] = "stale running task recovered"
                item["updated_at"] = now
                changed = True
        if changed:
            self.store.write("tasks/index.json", records[-500:])
        for item in records:
            if item.get("status") == "queued":
                return item
        return None

    def update(self, task_id, status, result="", error=""):
        records = self._records()
        for item in records:
            if item.get("id") == str(task_id):
                item["status"] = status
                item["result"] = str(result)[:6000]
                item["error"] = str(error)[:1000]
                if status == "running":
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                item["updated_at"] = time.time()
                self.store.write("tasks/index.json", records[-500:])
                return item
        return None
