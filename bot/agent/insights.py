"""Durable Agent reflections with confidence and provenance."""

import time
import uuid


class AgentInsightStore:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return "insights/{}.json".format(scope_key.replace(":", "_"))

    def add(self, scope_key, content, *, category="reflection", confidence=0.5, evidence="", source_id=""):
        content = str(content).strip()
        if not content:
            return None
        def add(records):
            if not isinstance(records, list):
                records = []
            for item in records:
                if item.get("content") == content[:1000] and item.get("category") == category:
                    item["confidence"] = max(float(item.get("confidence", 0)), float(confidence))
                    item["updated_at"] = time.time()
                    return records[-200:], item
            now = time.time()
            item = {
                "id": uuid.uuid4().hex[:12], "content": content[:1000],
                "category": str(category)[:50],
                "confidence": max(0.0, min(float(confidence), 1.0)),
                "evidence": str(evidence)[:2000], "source_id": str(source_id)[:100],
                "created_at": now, "updated_at": now,
            }
            return (records + [item])[-200:], item

        return self.store.update(self._path(scope_key), [], add)

    def list(self, scope_key, limit=30, category=None):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return []
        if category:
            records = [item for item in records if item.get("category") == category]
        records.sort(key=lambda item: (float(item.get("confidence", 0)), item.get("updated_at", 0)), reverse=True)
        return records[:max(1, min(int(limit), 100))]
