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
        def change(profile):
            if not isinstance(profile, dict):
                profile = {}
            if persona is not None:
                profile["persona"] = str(persona).strip()[:2000]
            if customs is not None:
                profile["customs"] = str(customs).strip()[:3000]
            if proactive_topics is not None:
                profile["proactive_topics"] = [
                    str(item).strip()[:120] for item in proactive_topics if str(item).strip()
                ][:30]
            profile["updated_at"] = time.time()
            return profile, profile

        return self.store.update(self._path(scope_key), {}, change)

    def append_custom(self, scope_key, text, max_items=10):
        """Append one auto-learned custom line with dedupe and a bound.

        人工写入的行不回写、不淘汰；超出上限时只移除此前自动回写的条目。
        """
        text = str(text).strip()[:200]
        if not text:
            return None

        def change(profile):
            if not isinstance(profile, dict):
                profile = {}
            lines = [
                line.strip() for line in str(profile.get("customs") or "").split("\n")
                if line.strip()
            ]
            auto = [
                str(item) for item in profile.get("auto_customs", [])
                if str(item) in lines
            ]
            if text not in lines:
                lines.append(text)
                auto = (auto + [text])[-max(1, int(max_items)):]
                while len(lines) > max(1, int(max_items)) and auto:
                    victim = auto.pop(0)
                    if victim in lines:
                        lines.remove(victim)
                profile["customs"] = "\n".join(lines)[:3000]
                profile["auto_customs"] = auto
            profile["updated_at"] = time.time()
            return profile, profile

        return self.store.update(self._path(scope_key), {}, change)

    def record_insight_writebacks(self, scope_key, insight_ids, max_items=50):
        """Record which insight ids were already written back into customs."""
        ids = [str(item) for item in insight_ids if str(item).strip()]
        if not ids:
            return None

        def change(profile):
            if not isinstance(profile, dict):
                profile = {}
            existing = [
                str(item) for item in profile.get("insight_writebacks", [])
                if str(item).strip()
            ]
            merged = existing + [item for item in ids if item not in existing]
            profile["insight_writebacks"] = merged[-max(1, int(max_items)):]
            profile["updated_at"] = time.time()
            return profile, profile

        return self.store.update(self._path(scope_key), {}, change)
