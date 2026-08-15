"""Deterministic reminder records and due-delivery state."""

import time

from .storage.json_store import new_record_id


class AgentReminderStore:
    def __init__(self, store):
        self.store = store

    def _records(self):
        value = self.store.read("reminders/index.json", [])
        return value if isinstance(value, list) else []

    def create(self, scope_key, user_id, text, due_at):
        reminder = {
            "id": new_record_id(),
            "scope_key": scope_key,
            "user_id": int(user_id),
            "text": str(text).strip()[:1000],
            "due_at": float(due_at),
            "status": "pending",
            "attempts": 0,
            "created_at": time.time(),
        }
        def add(records):
            if not isinstance(records, list):
                records = []
            return (records + [reminder])[-1000:], reminder

        self.store.update("reminders/index.json", [], add)
        return reminder

    def list(self, scope_key, *, pending_only=True):
        records = [item for item in self._records() if item.get("scope_key") == scope_key]
        if pending_only:
            records = [item for item in records if item.get("status") == "pending"]
        return sorted(records, key=lambda item: float(item.get("due_at", 0)))

    def due(self, now=None, limit=20):
        now = time.time() if now is None else float(now)
        return [item for item in self._records()
                if item.get("status") == "pending" and float(item.get("due_at", 0)) <= now][:limit]

    def mark(self, reminder_id, status, error=""):
        def change(records):
            if not isinstance(records, list):
                records = []
            updated = None
            for item in records:
                if item.get("id") != str(reminder_id):
                    continue
                item["status"] = str(status)[:30]
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["last_error"] = str(error)[:300]
                item["updated_at"] = time.time()
                updated = item
                break
            return records[-1000:], updated

        return self.store.update("reminders/index.json", [], change)

    def cancel(self, scope_key, reminder_id):
        for item in self._records():
            if item.get("scope_key") == scope_key and item.get("id") == str(reminder_id):
                return self.mark(reminder_id, "cancelled")
        return None
