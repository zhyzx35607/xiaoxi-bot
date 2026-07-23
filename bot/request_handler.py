# bot/request_handler.py - Friend/group request handling
import json
import logging
import os
import time

from .guard import is_blacklisted
from .permission import is_group_enabled
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PENDING_PATH = os.path.join(_ROOT, "data", "pending_requests.json")


def load_pending_requests():
    try:
        with open(_PENDING_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending_requests(data):
    os.makedirs(os.path.dirname(_PENDING_PATH), exist_ok=True)
    atomic_write_json(_PENDING_PATH, data, indent=2)


def _short_flag(flag):
    return str(flag or "")[-10:] or "unknown"


def format_pending_requests(limit=10):
    pending = load_pending_requests()
    if not pending:
        return "本地没有待处理申请"

    entries = sorted(
        pending.values(),
        key=lambda item: item.get("ts", 0),
        reverse=True,
    )[:limit]
    lines = ["本地待处理申请（{} 条，显示最近 {} 条）".format(len(pending), len(entries))]
    for item in entries:
        req_type = item.get("request_type") or "unknown"
        title = "好友" if req_type == "friend" else "入群"
        flag = item.get("flag", "")
        short_flag = _short_flag(flag)
        group_id = item.get("group_id") or ""
        user_id = item.get("user_id") or ""
        comment = (item.get("comment") or "").replace("\n", " ")[:80]
        parts = [title, "flag尾号:" + short_flag, "QQ:" + str(user_id)]
        if group_id:
            parts.append("群:" + str(group_id))
        if comment:
            parts.append("验证:" + comment)
        lines.append("  " + " | ".join(parts))
        lines.append("    /approve {} 或 /reject {} 原因".format(short_flag, short_flag))
    return "\n".join(lines)


async def handle_request(dispatcher, event):
    req_type = event.get("request_type", "")
    flag = event.get("flag", "")
    user_id = event.get("user_id", 0)
    group_id = event.get("group_id", 0)
    comment = event.get("comment", "") or ""
    sub_type = event.get("sub_type", "")

    request_log = log.info
    if req_type == "group" and group_id and not is_group_enabled(dispatcher, group_id):
        # Keep the request available for owner approval without filling the
        # normal log with events from groups the bot has not enabled.
        request_log = log.debug
    request_log("Request event type=%s subtype=%s group=%s user=%s flag=%s comment=%s",
                req_type, sub_type, group_id, user_id, _short_flag(flag), comment[:80])

    if req_type == "group" and group_id and is_blacklisted(group_id, user_id):
        reason = "黑名单用户"
        await dispatcher.client.set_group_add_request(flag, sub_type, False, reason)
        log.info("Rejected blacklisted group request: g=%s u=%s", group_id, user_id)
        return

    pending = load_pending_requests()
    pending[str(flag)] = {
        "ts": time.time(),
        "request_type": req_type,
        "sub_type": sub_type,
        "group_id": group_id,
        "user_id": user_id,
        "comment": comment,
        "flag": flag,
    }
    # Keep recent 80 entries only.
    if len(pending) > 80:
        newest = sorted(pending.items(), key=lambda item: item[1].get("ts", 0), reverse=True)[:80]
        pending = dict(newest)
    save_pending_requests(pending)

    if dispatcher.config.get("notify_owner_on_request", False):
        owner = dispatcher.config.get("bot_owner")
        if owner:
            text = _format_owner_notice(req_type, sub_type, group_id, user_id, comment, flag)
            await dispatcher.client.send_private_msg(owner, text)
    else:
        request_log("Request stored for owner pull: type=%s group=%s user=%s flag=%s",
                    req_type, group_id, user_id, _short_flag(flag))


def _format_owner_notice(req_type, sub_type, group_id, user_id, comment, flag):
    title = "收到好友请求" if req_type == "friend" else "收到入群请求"
    lines = [title]
    if group_id:
        lines.append("群号：" + str(group_id))
    lines.append("QQ：" + str(user_id))
    if sub_type:
        lines.append("类型：" + str(sub_type))
    if comment:
        lines.append("验证：" + str(comment)[:200])
    lines.append("flag：" + str(flag))
    lines.append("")
    lines.append("/approve " + str(flag))
    lines.append("/reject " + str(flag) + " 原因")
    return "\n".join(lines)


async def approve_request(dispatcher, flag, approve=True, reason=""):
    pending = load_pending_requests()
    entry = pending.get(str(flag))
    if not entry:
        # Allow using the tail of a long flag from QQ private chat.
        matches = [v for k, v in pending.items() if k.endswith(str(flag))]
        entry = matches[0] if len(matches) == 1 else None
        if entry:
            flag = entry.get("flag", flag)
    if not entry:
        return False, "没找到这个请求，可能已经处理过了"

    req_type = entry.get("request_type")
    if req_type == "friend":
        result = await dispatcher.client.set_friend_add_request(flag, approve, "" if approve else reason)
    elif req_type == "group":
        result = await dispatcher.client.set_group_add_request(
            flag,
            entry.get("sub_type", "add"),
            approve,
            "" if approve else reason,
        )
    else:
        return False, "请求类型不认识"

    if result.get("status") == "ok":
        pending.pop(str(entry.get("flag", flag)), None)
        save_pending_requests(pending)
        return True, "处理好了"
    return False, result.get("msg") or result.get("wording") or str(result)
