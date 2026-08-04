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
        return self.store.append_bounded(self._path(candidate.scope_key, bucket), {
            "scope_key": candidate.scope_key,
            "subject_id": candidate.subject_id,
            "content": candidate.content,
            "confidence": candidate.confidence,
            "source_event_id": candidate.source_event_id,
        }, limit=100)

    def list_records(self, scope_key, confirmed=True):
        return self.store.read(self._path(scope_key, "confirmed" if confirmed else "pending"), [])

    def confirm(self, scope_key, index):
        pending_path = self._path(scope_key, "pending")
        pending = self.store.read(pending_path, [])
        if not isinstance(pending, list) or index < 0 or index >= len(pending):
            return False
        item = pending.pop(index)
        confirmed = self.store.read(self._path(scope_key, "confirmed"), [])
        if not isinstance(confirmed, list):
            confirmed = []
        confirmed.append(item)
        self.store.write(pending_path, pending[-100:])
        self.store.write(self._path(scope_key, "confirmed"), confirmed[-100:])
        return True
