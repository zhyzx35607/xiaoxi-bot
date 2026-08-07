"""Evidence-based memory with strict private/group scope isolation."""

from .models import AgentEvent, MemoryCandidate


class AgentMemory:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key, bucket):
        safe = scope_key.replace(":", "_").replace("/", "_")
        return f"memory/{safe}/{bucket}.json"

    def add_candidate(self, candidate: MemoryCandidate):
        bucket = "pending" if candidate.requires_confirmation else "confirmed"
        path = self._path(candidate.scope_key, bucket)
        def add(records):
            if not isinstance(records, list):
                records = []
            normalized = " ".join(candidate.content.lower().split())
            for item in records:
                if (int(item.get("subject_id") or 0) == int(candidate.subject_id or 0)
                        and " ".join(str(item.get("content", "")).lower().split()) == normalized):
                    return records, item
            item = {
                "scope_key": candidate.scope_key,
                "subject_id": candidate.subject_id,
                "content": candidate.content,
                "confidence": candidate.confidence,
                "source_event_id": candidate.source_event_id,
                "category": candidate.category,
            }
            return (records + [item])[-100:], item

        return self.store.update(path, [], add)

    def list_records(self, scope_key, confirmed=True):
        return self.store.read(self._path(scope_key, "confirmed" if confirmed else "pending"), [])

    def confirm(self, scope_key, index):
        pending_path = self._path(scope_key, "pending")
        confirmed_path = self._path(scope_key, "confirmed")

        def move(values):
            pending = values[pending_path]
            confirmed = values[confirmed_path]
            if not isinstance(pending, list) or index < 0 or index >= len(pending):
                return values, False
            item = pending.pop(index)
            if not isinstance(confirmed, list):
                confirmed = []
            values[pending_path] = pending[-100:]
            values[confirmed_path] = (confirmed + [item])[-100:]
            return values, True

        return self.store.update_many(
            {pending_path: [], confirmed_path: []},
            move,
        )
