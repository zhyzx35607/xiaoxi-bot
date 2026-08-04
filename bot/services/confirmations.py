"""Short-lived confirmation records for destructive NapCat actions."""

import json
import os
import secrets
import time

from ..utils import atomic_write_json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PATH = os.path.join(_ROOT, "data", "pending_actions.json")
_TTL = 60


def _load():
    try:
        with open(_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    atomic_write_json(_PATH, data, indent=2)


def _prune(data):
    now = time.time()
    for code, item in list(data.items()):
        if not isinstance(item, dict) or float(item.get("expires_at", 0)) <= now:
            data.pop(code, None)


def create_confirmation(group_id, user_id, action, params, description):
    data = _load()
    _prune(data)
    code = secrets.token_hex(3)
    data[code] = {
        "group_id": int(group_id or 0),
        "user_id": int(user_id),
        "action": str(action),
        "params": params if isinstance(params, dict) else {},
        "description": str(description)[:200],
        "created_at": time.time(),
        "expires_at": time.time() + _TTL,
    }
    _save(data)
    return code


def cancel_confirmation(code, user_id):
    data = _load()
    _prune(data)
    item = data.get(str(code))
    if not item or int(item.get("user_id", 0)) != int(user_id):
        _save(data)
        return False
    data.pop(str(code), None)
    _save(data)
    return True


async def execute_confirmation(dispatcher, code, user_id, group_id, role):
    from ..permission import LEVEL_ADMIN, get_user_level
    data = _load()
    _prune(data)
    item = data.get(str(code))
    if not item:
        _save(data)
        return False, "确认码不存在或已经过期"
    if int(item.get("group_id", 0)) != int(group_id or 0):
        return False, "这个确认码不属于当前群"
    level, _ = await get_user_level(dispatcher, group_id, user_id, role)
    if level < LEVEL_ADMIN or int(item.get("user_id", 0)) != int(user_id):
        return False, "只能由发起操作的管理员确认"
    action = item.get("action")
    params = item.get("params") or {}
    data.pop(str(code), None)
    _save(data)
    result = await dispatcher.client.call(action, params)
    if isinstance(result, dict) and result.get("status") == "ok":
        return True, "执行好了：" + str(item.get("description") or action)
    return False, "执行失败：" + str((result or {}).get("message") or (result or {}).get("msg") or result)[:160]
