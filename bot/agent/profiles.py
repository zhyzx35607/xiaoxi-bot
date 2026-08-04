"""Group- or owner-specific identity, customs, and proactive topics."""

import time


class AgentProfileStore:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return "profiles/{}.json".format(scope_key.replace(":", "_"))

    def get(self, scope_key):
        value = self.store.read(self._path(scope_key), {})
        return value if isinstance(value, dict) else {}

    def update(self, scope_key, *, persona=None, customs=None, proactive_topics=None):
        profile = self.get(scope_key)
        if persona is not None:
            profile["persona"] = str(persona).strip()[:2000]
        if customs is not None:
            profile["customs"] = str(customs).strip()[:3000]
        if proactive_topics is not None:
            profile["proactive_topics"] = [str(item).strip()[:120] for item in proactive_topics if str(item).strip()][:30]
        profile["updated_at"] = time.time()
        self.store.write(self._path(scope_key), profile)
        return profile
