"""Persistent multi-step execution plans scoped to an owner or group."""

import time
import uuid


TERMINAL_STEP_STATUSES = {"done", "failed", "cancelled", "skipped"}


class AgentPlanStore:
    def __init__(self, store):
        self.store = store

    def _path(self, scope_key):
        return "plans/{}.json".format(scope_key.replace(":", "_"))

    def create(self, scope_key, owner_id, title, steps, success_criteria="", source_event_id=""):
        now = time.time()
        normalized = []
        for index, step in enumerate(steps[:20], 1):
            if isinstance(step, str):
                text, criteria = step, ""
            elif isinstance(step, dict):
                text = step.get("title") or step.get("step") or ""
                criteria = step.get("success_criteria") or ""
            else:
                continue
            text = str(text).strip()
            if text:
                normalized.append({
                    "id": "s{}".format(index), "title": text[:500],
                    "success_criteria": str(criteria)[:500], "status": "pending",
                    "evidence": "", "result": "", "updated_at": now,
                })
        record = {
            "id": uuid.uuid4().hex[:12], "scope_key": scope_key,
            "owner_id": int(owner_id or 0), "title": str(title).strip()[:1000],
            "success_criteria": str(success_criteria).strip()[:1000],
            "status": "active" if normalized else "draft", "steps": normalized,
            "source_event_id": str(source_event_id)[:80],
            "created_at": now, "updated_at": now,
        }
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            records = []
        records.append(record)
        self.store.write(self._path(scope_key), records[-100:])
        return record

    def list(self, scope_key, statuses=None):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return []
        if statuses:
            records = [item for item in records if item.get("status") in statuses]
        return sorted(records, key=lambda item: item.get("updated_at", 0), reverse=True)

    def get(self, scope_key, plan_id):
        return next((item for item in self.list(scope_key) if item.get("id") == plan_id), None)

    def update_step(self, scope_key, plan_id, step_id, status, *, evidence="", result=""):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return None
        updated = None
        now = time.time()
        for plan in records:
            if plan.get("id") != plan_id:
                continue
            step_found = False
            for step in plan.get("steps", []):
                if step.get("id") == step_id:
                    step_found = True
                    step["status"] = str(status)[:30]
                    if evidence:
                        step["evidence"] = str(evidence)[:2000]
                    if result:
                        step["result"] = str(result)[:2000]
                    step["updated_at"] = now
                    break
            if not step_found:
                return None
            statuses = [step.get("status") for step in plan.get("steps", [])]
            if statuses and all(item in TERMINAL_STEP_STATUSES for item in statuses):
                plan["status"] = "failed" if "failed" in statuses else "done"
            elif any(item == "running" for item in statuses):
                plan["status"] = "running"
            else:
                plan["status"] = "active"
            plan["updated_at"] = now
            updated = plan
            break
        if updated:
            self.store.write(self._path(scope_key), records[-100:])
        return updated

    def cancel(self, scope_key, plan_id):
        records = self.store.read(self._path(scope_key), [])
        if not isinstance(records, list):
            return None
        now = time.time()
        updated = None
        for plan in records:
            if plan.get("id") != plan_id or plan.get("status") in {"done", "failed", "cancelled"}:
                continue
            plan["status"] = "cancelled"
            plan["updated_at"] = now
            for step in plan.get("steps", []):
                if step.get("status") not in TERMINAL_STEP_STATUSES:
                    step["status"] = "cancelled"
                    step["updated_at"] = now
            updated = plan
            break
        if updated:
            self.store.write(self._path(scope_key), records[-100:])
        return updated
