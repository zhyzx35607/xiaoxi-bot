# bot/request_handler.py - Friend/group request handling
import json
import logging
import os
import time

from app.logging_setup import sanitize_log_message

from ..guard import is_blacklisted
from ..permission import (
    LEVEL_ADMIN,
    get_bot_role,
    get_user_level,
    is_group_enabled,
)
from ..utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PENDING_PATH = os.path.join(_ROOT, "data", "pending_requests.json")
# 群内审批公告的有效期：管理员需在公告发出后 10 分钟内回复
JOIN_REVIEW_TTL_SECONDS = 600
# 「同意吧」「拒绝了」这类语气词不影响确定性匹配
_REVIEW_MODAL_PARTICLES = "吧了呢啊呀哦嘛啦"


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
    visible = [item for item in pending.values() if not item.get("handled")]
    if not visible:
        return "本地没有待处理申请"

    entries = sorted(
        visible,
        key=lambda item: item.get("ts", 0),
        reverse=True,
    )[:limit]
    lines = ["本地待处理申请（{} 条，显示最近 {} 条）".format(len(visible), len(entries))]
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


def _join_review_enabled(dispatcher, group_id):
    """按群配置开关：groups.<gid>.join_review，默认关闭。"""
    groups = dispatcher.config.get("groups", {})
    group_cfg = groups.get(str(group_id), {})
    if not isinstance(group_cfg, dict):
        return False
    return bool(group_cfg.get("join_review", False))


async def _announce_join_review(dispatcher, event, flag):
    """群内公告入群申请，供管理员回复「同意/拒绝」做确定性审批。

    只有该群已启用、join_review 开启且 bot 实时角色是群主/管理时才公告；
    公告成功后把 message_id 和过期时间写回 pending 条目。
    """
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    comment = str(event.get("comment") or "")
    if not is_group_enabled(dispatcher, group_id) or not _join_review_enabled(dispatcher, group_id):
        return
    bot_role, _ = await get_bot_role(dispatcher, group_id)
    if bot_role not in ("owner", "admin"):
        log.debug("Join review announce skipped: bot role=%s group=%s", bot_role, group_id)
        return
    text = "\n".join([
        "入群申请待审批",
        "申请人 QQ：" + str(user_id),
        "验证消息：" + (comment.replace("\n", " ")[:200] or "（无）"),
        "管理员及以上 10 分钟内回复「同意」或「拒绝」即可处理；"
        "回复（引用）本条消息可指定审批哪一条申请。",
    ])
    try:
        result = await dispatcher.client.send_group_msg(group_id, text)
    except Exception:
        log.exception("Join review announce failed group=%s", group_id)
        return
    if not isinstance(result, dict) or result.get("status") not in (None, "ok"):
        log.warning("Join review announce rejected group=%s", group_id)
        return
    message_id = (result.get("data") or {}).get("message_id")
    pending = load_pending_requests()
    entry = pending.get(str(flag))
    if entry is None:
        return
    entry["announce_message_id"] = message_id
    entry["announce_expires_at"] = time.time() + JOIN_REVIEW_TTL_SECONDS
    save_pending_requests(pending)
    log.info("Join review announced group=%s user=%s flag=%s",
             group_id, user_id, _short_flag(flag))


def _match_review_intent(text):
    """纯文本「同意/拒绝」匹配，允许前后空白和尾部语气词。返回 True/False/None。"""
    value = str(text or "").strip()
    while value and value[-1] in _REVIEW_MODAL_PARTICLES:
        value = value[:-1].rstrip()
    if value == "同意":
        return True
    if value == "拒绝":
        return False
    return None


def _pending_review_candidates(pending, group_id, now=None):
    """该群未处理且未过期的入群申请。"""
    now = now if now is not None else time.time()
    candidates = []
    for entry in pending.values():
        if entry.get("request_type") != "group":
            continue
        if int(entry.get("group_id") or 0) != int(group_id):
            continue
        if entry.get("handled"):
            continue
        expires = entry.get("announce_expires_at")
        if expires is not None and now > float(expires):
            continue
        candidates.append(entry)
    return candidates


async def try_handle_join_review(dispatcher, event):
    """群内入群审批：管理员发纯文本「同意/拒绝」处理待审批申请。

    命中并消费该消息时返回 True；不介入（交给后续流程）返回 False。
    确定性匹配 + 实时身份核验，不依赖 LLM 判断。
    """
    if event.get("message_type") != "group":
        return False
    group_id = event.get("group_id", 0)
    if not group_id or not _join_review_enabled(dispatcher, group_id):
        return False
    message = event.get("message") or []
    if isinstance(message, str):
        segments = [{"type": "text", "data": {"text": message}}]
    else:
        segments = message
    reply_id = None
    text_parts = []
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        seg_type = segment.get("type")
        if seg_type == "reply":
            reply_id = (segment.get("data") or {}).get("id")
        elif seg_type == "text":
            text_parts.append(str((segment.get("data") or {}).get("text") or ""))
        else:
            # 含图片/表情等其他段的不是纯文本审批
            return False
    intent = _match_review_intent("".join(text_parts))
    if intent is None:
        return False
    candidates = _pending_review_candidates(load_pending_requests(), group_id)
    if not candidates:
        return False
    # 实时核验发言者身份，QQ 侧角色以 API 为准
    user_id = event.get("user_id", 0)
    sender_role = (event.get("sender") or {}).get("role", "member")
    level, _ = await get_user_level(dispatcher, group_id, user_id, sender_role)
    if level < LEVEL_ADMIN:
        return False
    if reply_id:
        target = next(
            (entry for entry in candidates
             if str(entry.get("announce_message_id") or "") == str(reply_id)),
            None)
        if target is None:
            # 回复的不是审批公告，交给后续流程
            return False
    elif len(candidates) > 1:
        await dispatcher.client.send_group_msg(
            group_id, "有多条待审批的入群申请，请回复对应那条审批消息再处理")
        return True
    else:
        target = candidates[0]
    flag = str(target.get("flag") or "")
    approve = intent
    reason = "" if approve else "群内管理员拒绝"
    try:
        result = await dispatcher.client.set_group_add_request(
            flag, target.get("sub_type") or "add", approve, reason)
    except Exception as error:
        await dispatcher.client.send_group_msg(
            group_id, "审批处理失败：" + str(error)[:120])
        return True
    if not isinstance(result, dict) or result.get("status") not in (None, "ok"):
        err = (result.get("msg") or result.get("wording") or str(result)
               if isinstance(result, dict) else str(result))
        await dispatcher.client.send_group_msg(
            group_id, "审批处理失败：" + str(err)[:120])
        return True
    pending = load_pending_requests()
    entry = pending.get(flag)
    if entry is not None:
        entry["handled"] = True
        entry["handled_by"] = user_id
        entry["handled_at"] = time.time()
        entry["handled_action"] = "approve" if approve else "reject"
        save_pending_requests(pending)
    await dispatcher.client.send_group_msg(
        group_id,
        ("已同意" if approve else "已拒绝")
        + " QQ " + str(target.get("user_id")) + " 的入群申请")
    log.info("Join review handled group=%s actor=%s target=%s action=%s flag=%s",
             group_id, user_id, target.get("user_id"),
             "approve" if approve else "reject", _short_flag(flag))
    return True


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
    # 验证消息是用户输入：脱敏并去掉换行，避免日志注入和 QQ 号直记
    safe_comment = sanitize_log_message(comment.replace("\n", " "), limit=80)
    request_log("Request event type=%s subtype=%s group=%s user=%s flag=%s comment=%s",
                req_type, sub_type, group_id, user_id, _short_flag(flag), safe_comment)

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

    if req_type == "group" and group_id:
        await _announce_join_review(dispatcher, event, flag)

    if req_type == "group" and group_id and is_group_enabled(dispatcher, group_id):
        # 入群申请只 observe 进 Agent 事件流，不触发决策
        agent_settings = dispatcher.config.get("agent", {})
        runtime = getattr(dispatcher, "agent_runtime", None)
        if agent_settings.get("observation_enabled", False) and runtime is not None:
            try:
                runtime.observe({
                    "post_type": "request",
                    "user_id": user_id,
                    "group_id": group_id,
                    "message_type": "group",
                    "raw_message": "[request] 用户{} 申请加入本群（{}），验证消息：{}".format(
                        user_id, sub_type, safe_comment),
                    "time": event.get("time", time.time()),
                    "sender": {"role": "member"},
                })
            except Exception:
                log.exception("Agent request observation failed")

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
    # 群内审批已处理的条目保留在 pending 里供追溯，但不再重复执行
    if entry.get("handled"):
        return True, "这个申请已经在群里审批过了，不用重复处理"

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
