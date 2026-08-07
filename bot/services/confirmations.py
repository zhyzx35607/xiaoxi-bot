"""Short-lived confirmation records for destructive NapCat actions."""

import asyncio
import json
import os
import secrets
import tarfile
import threading
import time

from ..memory import sanitize_for_memory, sanitize_persistent_value
from ..utils import atomic_write_json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PATH = os.path.join(_ROOT, "data", "pending_actions.json")
_TTL = 60
_LOCK = threading.RLock()

def _load_unlocked():
    try:
        with open(_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _load():
    with _LOCK:
        return _load_unlocked()


def _save_unlocked(data):
    atomic_write_json(_PATH, data, indent=2)

def _save(data):
    with _LOCK:
        _save_unlocked(data)


def _prune(data):
    now = time.time()
    for code, item in list(data.items()):
        try:
            expires_at = float(item.get("expires_at", 0)) if isinstance(item, dict) else 0
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at <= now:
            data.pop(code, None)

def prune_expired_confirmations():
    """Remove expired records during dispatcher startup and maintenance."""
    with _LOCK:
        data = _load_unlocked()
        before = len(data)
        _prune(data)
        if len(data) != before:
            _save_unlocked(data)
        return before - len(data)


def create_confirmation(group_id, user_id, action, params, description):
    with _LOCK:
        data = _load_unlocked()
        _prune(data)
        code = secrets.token_hex(3)
        data[code] = {
            "group_id": int(group_id or 0),
            "user_id": int(user_id),
            "action": str(action),
            "params": sanitize_persistent_value(params if isinstance(params, dict) else {}),
            "description": sanitize_for_memory(str(description)[:200]),
            "created_at": time.time(),
            "expires_at": time.time() + _TTL,
        }
        _save_unlocked(data)
        return code


def create_agent_confirmation(group_id, user_id, event, plan, description):
    return create_confirmation(
        group_id, user_id, "__agent_plan__",
        {
            "event": event if isinstance(event, dict) else {},
            "plan": plan if isinstance(plan, dict) else {},
        },
        description,
    )


def cancel_confirmation(code, user_id):
    with _LOCK:
        data = _load_unlocked()
        _prune(data)
        item = data.get(str(code))
        if not item or int(item.get("user_id", 0)) != int(user_id):
            _save_unlocked(data)
            return False
        data.pop(str(code), None)
        _save_unlocked(data)
        return True


async def execute_confirmation(dispatcher, code, user_id, group_id, role):
    from ..permission import LEVEL_ADMIN, get_user_level
    with _LOCK:
        data = _load_unlocked()
        _prune(data)
        item = data.get(str(code))
        if not item:
            _save_unlocked(data)
            return False, "确认码不存在或已经过期"
        if int(item.get("group_id", 0)) != int(group_id or 0):
            _save_unlocked(data)
            return False, "这个确认码不属于当前群"
    level, _ = await get_user_level(dispatcher, group_id, user_id, role)
    with _LOCK:
        data = _load_unlocked()
        _prune(data)
        item = data.get(str(code))
        if not item:
            _save_unlocked(data)
            return False, "确认码不存在或已经过期"
        if int(item.get("group_id", 0)) != int(group_id or 0):
            _save_unlocked(data)
            return False, "这个确认码不属于当前群"
        if level < LEVEL_ADMIN or int(item.get("user_id", 0)) != int(user_id):
            _save_unlocked(data)
            return False, "只能由发起操作的管理员确认"
        action = item.get("action")
        params = sanitize_persistent_value(item.get("params") or {})
        description = item.get("description")
        if action == "__clear_group_data__":
            raw_targets = params.get("group_ids")
            raw_scopes = params.get("scopes", ["memory", "stickers", "guard"])
            targets = list(dict.fromkeys(
                str(value) for value in raw_targets
                if str(value).isdigit()
            )) if isinstance(raw_targets, list) else []
            scopes = list(dict.fromkeys(str(value) for value in raw_scopes)) \
                if isinstance(raw_scopes, list) else []
            configured = {str(value) for value in dispatcher.config.get("groups", {})}
            valid_scope = (
                targets
                and all(value in configured for value in targets)
                and scopes
                and set(scopes).issubset({"memory", "stickers", "guard"})
                and (
                    (group_id and targets == [str(group_id)])
                    or (not group_id and user_id == dispatcher.config.get("bot_owner"))
                )
            )
            if not valid_scope:
                data.pop(str(code), None)
                _save_unlocked(data)
                return False, "清理目标校验失败，操作已取消"
            params = {"group_ids": targets, "scopes": scopes}
        data.pop(str(code), None)
        _save_unlocked(data)
    if action == "__agent_plan__":
        result = await dispatcher.agent_runtime.execute_confirmed_plan(
            dispatcher, params.get("event") or {}, params.get("plan") or {}, role=role)
        if result.get("success"):
            return True, str(result.get("message") or "Agent 方案已确认并执行")[:3500]
        return False, "Agent 方案执行失败：" + sanitize_for_memory(str(result.get("reason") or "未知原因"))[:500]
    if action == "__clear_group_data__":
        from .group_data import clear_group_data, prepare_group_data_clear
        try:
            prepare_group_data_clear(params["group_ids"], params["scopes"])
            result = await asyncio.to_thread(
                clear_group_data, dispatcher, params["group_ids"], params["scopes"])
        except (OSError, ValueError, tarfile.TarError):
            return False, "清理失败，操作备份或数据写入未完成"
        return True, "已清理 {} 个群，操作备份：{}".format(
            len(result["group_ids"]), result["backup_name"])
    result = await dispatcher.client.call(action, params)
    if isinstance(result, dict) and result.get("status") == "ok":
        return True, "执行好了：" + str(description or action)
    return False, "执行失败：" + sanitize_for_memory(
        str((result or {}).get("message") or (result or {}).get("msg") or result)
    )[:160]
