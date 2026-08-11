"""Scope-isolated timeline of decisions, actions, evidence, and outcomes."""

import time


class AgentTimeline:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return "timeline/{}.json".format(scope_key.replace(":", "_"))

    def add(self, scope_key, kind, summary, *, actor_id=0, evidence="", metadata=None):
        item = {
            "timestamp": time.time(), "kind": str(kind)[:50],
            "summary": str(summary)[:1000], "actor_id": int(actor_id or 0),
            "evidence": str(evidence)[:2000],
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        event_id = item["metadata"].get("event_id")
        if event_id:
            item["dedupe_key"] = "{}:{}".format(item["kind"], event_id)
            self.store.append_bounded_unique(
                self._path(scope_key), item, key="dedupe_key", limit=500)
        else:
            self.store.append_bounded(self._path(scope_key), item, limit=500)
        return item

    def list(self, scope_key, limit=30, kinds=None):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return []
        if kinds:
            records = [item for item in records if item.get("kind") in kinds]
        return records[-max(1, min(int(limit), 100)):]
