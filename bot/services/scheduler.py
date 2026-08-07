"""bot/scheduler.py - Scheduled tasks (morning/evening greetings, cleanup, check-in).

Currently lightweight by design — on low-resource servers, avoid heavy
periodic work. Only started when config.runtime.enable_scheduler is true.
"""

import asyncio
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ..utils import atomic_write_json

log = logging.getLogger("qqbot")
_TZ_WARNING_NAMES = set()
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CHECKIN_STATUS_PATH = os.path.join(_ROOT, "data", "checkin_status.json")


def _timezone(name="Asia/Shanghai"):
    try:
        return ZoneInfo(str(name or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        key = str(name or "Asia/Shanghai")
        if key not in _TZ_WARNING_NAMES:
            _TZ_WARNING_NAMES.add(key)
            log.info("Timezone database unavailable for %s; using fixed UTC+8", name)
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def _scheduler_timezone(dispatcher):
    runtime = dispatcher.config.get("runtime", {})
    return str(runtime.get("scheduler_timezone") or "Asia/Shanghai")


def _client_connected(dispatcher):
    client = getattr(dispatcher, "client", None)
    connected = getattr(client, "is_connected", None)
    return True if connected is None else bool(connected)


def _seconds_until_next_checkin(timezone_name="Asia/Shanghai"):
    """Calculate seconds until the next 00:00:01 in the configured timezone."""
    try:
        now = datetime.now(_timezone(timezone_name))
    except TypeError:
        now = datetime.now()
    target = now.replace(hour=0, minute=0, second=1, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# Backward-compatible alias for existing tests and callers.
def _seconds_until_next_midnight():
    return _seconds_until_next_checkin()


async def _daily_checkin(dispatcher):
    """Check in at 00:00:01 for groups explicitly enabled in config."""
    group_list = _enabled_group_ids(dispatcher)
    if not group_list:
        log.info("Daily check-in skipped: no explicitly enabled groups")
        return {}
    return await _run_group_checkin(dispatcher, group_list, trigger="daily")


def _enabled_group_ids(dispatcher):
    groups = dispatcher.config.get("groups", {})
    return sorted(
        str(gid) for gid, group_cfg in groups.items()
        if isinstance(group_cfg, dict) and group_cfg.get("enabled") is True
    )


def _load_checkin_status():
    try:
        with open(_CHECKIN_STATUS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_checkin_status(dispatcher):
    state = _load_checkin_status()
    state["enabled_groups"] = _enabled_group_ids(dispatcher)
    state["next_run"] = time.time() + _seconds_until_next_checkin(
        _scheduler_timezone(dispatcher))
    return state


def format_checkin_status(dispatcher):
    def _time_text(timestamp):
        if not timestamp:
            return "暂无"
        return time.strftime("%m-%d %H:%M:%S", time.localtime(timestamp))

    state = get_checkin_status(dispatcher)
    lines = [
        "群打卡状态",
        "下次执行：{}".format(_time_text(state.get("next_run"))),
        "最近执行：{}".format(_time_text(state.get("last_run"))),
    ]
    results = state.get("groups", {}) if isinstance(state.get("groups"), dict) else {}
    enabled = state.get("enabled_groups", [])
    if not enabled:
        lines.append("当前没有启用群。")
        return "\n".join(lines)
    lines.append("启用群：")
    for gid in enabled:
        item = results.get(str(gid), {})
        if not item:
            lines.append("  {}：暂无记录".format(gid))
            continue
        status = "成功" if item.get("ok") else "失败"
        detail = item.get("message", "")
        lines.append("  {}：{} {}{}".format(
            gid, status, _time_text(item.get("timestamp")),
            "（{}）".format(detail) if detail else "",
        ))
    lines.append("手动测试：/打卡测试 群号")
    return "\n".join(lines)


async def run_manual_checkin(dispatcher, group_id):
    gid = str(group_id)
    if gid not in _enabled_group_ids(dispatcher):
        return False, "这个群未启用，不执行打卡"
    results = await _run_group_checkin(dispatcher, [gid], trigger="manual")
    item = results.get(gid, {})
    if item.get("ok"):
        return True, "群 {} 原生打卡调用成功".format(gid)
    return False, "群 {} 打卡失败：{}".format(gid, item.get("message") or "未知错误")


async def _run_group_checkin(dispatcher, group_list, trigger):
    if not _client_connected(dispatcher):
        log.info("Group check-in skipped: OneBot is offline")
        return {}
    lock = getattr(dispatcher, "_checkin_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        dispatcher._checkin_lock = lock
    async with lock:
        state = _load_checkin_status()
        stored_results = state.get("groups", {})
        if not isinstance(stored_results, dict):
            stored_results = {}
        current_results = {}
        run_timestamp = time.time()
        for index, gid in enumerate(group_list):
            result = {"status": "failed", "retcode": -1, "message": "unknown"}
            try:
                result = await dispatcher.client.send_group_sign(int(gid))
                if not _api_succeeded(result):
                    await asyncio.sleep(5)
                    result = await dispatcher.client.send_group_sign(int(gid))
                if _api_succeeded(result):
                    log.info("Group check-in succeeded: group %s trigger=%s", gid, trigger)
                else:
                    log.warning(
                        "Group check-in failed: group=%s trigger=%s retcode=%s status=%s",
                        gid, trigger, result.get("retcode"), result.get("status"),
                    )
            except Exception as e:
                log.warning("Daily check-in failed for group %s: %s", gid, e)
                result = {"status": "failed", "retcode": -1, "message": str(e)[:120]}
            item = {
                "ok": _api_succeeded(result),
                "timestamp": time.time(),
                "trigger": trigger,
                "retcode": result.get("retcode", 0 if _api_succeeded(result) else -1),
                "message": str(result.get("message") or result.get("msg", ""))[:120],
            }
            current_results[str(gid)] = item
            stored_results[str(gid)] = item
            if index + 1 < len(group_list):
                await asyncio.sleep(2)
        state.update({
            "last_run": run_timestamp,
            "last_trigger": trigger,
            "groups": stored_results,
        })
        atomic_write_json(_CHECKIN_STATUS_PATH, state, indent=2)
        return current_results


def _api_succeeded(result):
    return (isinstance(result, dict) and result.get("status") == "ok"
            and result.get("retcode", 0) == 0)


def _seconds_until_time(hour, minute=0, timezone_name="Asia/Shanghai"):
    """Seconds until the next HH:MM:05 in the configured timezone."""
    try:
        now = datetime.now(_timezone(timezone_name))
    except TypeError:
        now = datetime.now()
    target = now.replace(hour=int(hour) % 24, minute=int(minute) % 60,
                         second=5, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


BOARD_NAMES = {
    "weibo": "微博", "zhihu": "知乎", "bilibili": "B站", "douyin": "抖音",
    "baidu": "百度", "toutiao": "头条", "ithome": "IT之家", "v2ex": "V2EX",
    "github": "GitHub", "36kr": "36氪", "douban-movie": "豆瓣电影",
}


def format_hotboard(board, items, limit=10, summary=None, details=None):
    """Format hot-board items into a readable plain-text fallback."""
    name = BOARD_NAMES.get(board, board)
    lines = ["【{}热榜】".format(name)]
    if summary:
        lines.append(str(summary).strip())
    for index, item in enumerate(items[:limit], 1):
        title = str(item.get("title") or "").strip()[:80]
        line = "{}. {}".format(index, title)
        hot = str(item.get("hot_value") or "").strip()
        if hot:
            line += "\uFF08" + hot + "\uFF09"
        lines.append(line)
        if details and index - 1 < len(details):
            detail = str(details[index - 1] or "").strip()
            if detail:
                lines.append(detail)
        sources = item.get("sources") if isinstance(item, dict) else None
        urls = sources if isinstance(sources, list) and sources else [item.get("url")]
        for url in urls[:2]:
            if url:
                lines.append(str(url).strip())
    return "\n".join(lines)


def build_hotboard_forward_nodes(board, items, bot_qq, limit=10, summary=None, details=None):
    """Build a detailed merged-forward digest with evidence links."""
    name = BOARD_NAMES.get(board, board)
    header = "【{}热榜】".format(name)
    if summary:
        header += "\n" + str(summary).strip()
    node_name = "小汐"
    node_uin = str(bot_qq)
    nodes = [{"type": "node", "data": {"name": node_name, "uin": node_uin, "content": header}}]
    for index, item in enumerate(items[:limit], 1):
        title = str(item.get("title") or "").strip()[:100] or "暂无标题"
        text = "{}. {}".format(index, title)
        hot = str(item.get("hot_value") or "").strip()
        if hot:
            text += "\uFF08" + hot + "\uFF09"
        if details and index - 1 < len(details):
            detail = str(details[index - 1] or "").strip()
            if detail:
                text += "\n" + detail
        sources = item.get("sources") if isinstance(item, dict) else None
        urls = sources if isinstance(sources, list) and sources else [item.get("url")]
        valid_urls = [str(url).strip() for url in urls[:2] if url]
        if valid_urls:
            text += "\n参考来源：\n" + "\n".join(valid_urls)
        nodes.append({"type": "node", "data": {"name": node_name, "uin": node_uin, "content": text}})
    return nodes


async def build_detailed_hotboard(dispatcher, board, items):
    from .hotboard_digest import build_hotboard_digest
    cfg = dispatcher.config.get("hotboard_push", {})
    limit = max(1, min(10, int(cfg.get("detail_count", 10) or 10)))
    name = BOARD_NAMES.get(board, board)
    return await build_hotboard_digest(dispatcher, board, name, items, limit=limit)


async def ai_hotboard_summary(dispatcher, board, items):
    """Compatibility helper returning the evidence-based overview only."""
    try:
        return (await build_detailed_hotboard(dispatcher, board, items)).get("summary")
    except Exception as error:
        log.info("hotboard AI summary failed: %s", error)
        return None

def _feature_on(dispatcher, gid, name):
    from ..permission import get_group_config
    gcfg = get_group_config(dispatcher, gid)
    return gcfg.get("features", {}).get(name, True)


_ACG_HISTORY_PATH = os.path.join(_ROOT, "data", "acg_history.json")

_DEFAULT_ACG_WINDOWS = [
    ("08:00", "11:00"),
    ("12:00", "15:00"),
    ("16:00", "19:00"),
    ("20:00", "23:00"),
]
_DEFAULT_HOTBOARD_WINDOWS = [("10:00", "13:00"), ("19:00", "22:00")]


def _new_acg_state():
    return {
        "version": 2,
        "recent": {},
        "pool": [],
        "pending_due": False,
        "delivery": None,
        "last_failure": None,
        "schedule": {"date": "", "acg": [], "hotboard": [], "done": {"acg": [], "hotboard": []}},
    }


def _load_acg_state():
    state = _new_acg_state()
    try:
        with open(_ACG_HISTORY_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return state
    if not isinstance(data, dict):
        return state
    if int(data.get("version", 1) or 1) < 2:
        legacy_pending = data.get("pending", {})
        pool = []
        if isinstance(legacy_pending, dict):
            for values in legacy_pending.values():
                if isinstance(values, list):
                    pool.extend(str(value) for value in values if isinstance(value, str))
        state["pool"] = list(dict.fromkeys(pool))[-200:]
        return state
    recent = data.get("recent", {})
    if isinstance(recent, dict):
        for url, timestamp in recent.items():
            try:
                state["recent"][str(url)] = float(timestamp)
            except (TypeError, ValueError):
                continue
    pool = data.get("pool", [])
    if isinstance(pool, list):
        state["pool"] = list(dict.fromkeys(
            str(value) for value in pool if isinstance(value, str)
        ))[-200:]
    state["pending_due"] = bool(data.get("pending_due", False))
    delivery = data.get("delivery")
    if isinstance(delivery, dict) and isinstance(delivery.get("urls"), list):
        delivery["attempts"] = delivery.get("attempts") if isinstance(delivery.get("attempts"), dict) else {}
        state["delivery"] = delivery
    if isinstance(data.get("last_failure"), dict):
        state["last_failure"] = data["last_failure"]
    schedule = data.get("schedule")
    if isinstance(schedule, dict):
        state["schedule"] = schedule
        state["schedule"].setdefault("date", "")
        state["schedule"].setdefault("acg", [])
        state["schedule"].setdefault("hotboard", [])
        done = state["schedule"].setdefault("done", {})
        done.setdefault("acg", [])
        done.setdefault("hotboard", [])
    return state


def _save_acg_state(state):
    atomic_write_json(_ACG_HISTORY_PATH, state, indent=2)


def _load_acg_history():
    return list(_load_acg_state()["recent"])


def _save_acg_history(urls, pending=None):
    """Compatibility writer for old callers and tests."""
    state = _load_acg_state()
    now = time.time()
    state["recent"] = {str(url): now for url in urls[-3000:] if isinstance(url, str)}
    if isinstance(pending, dict):
        values = []
        for pending_urls in pending.values():
            if isinstance(pending_urls, list):
                values.extend(str(url) for url in pending_urls if isinstance(url, str))
        state["pool"] = list(dict.fromkeys(values))[-200:]
    _save_acg_state(state)


def _state_lock(dispatcher):
    lock = getattr(dispatcher, "_acg_state_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        dispatcher._acg_state_lock = lock
    return lock


def _delivery_lock(dispatcher):
    lock = getattr(dispatcher, "_acg_delivery_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        dispatcher._acg_delivery_lock = lock
    return lock


def _acg_send_count(dispatcher):
    cfg = dispatcher.config.get("acg_images", {})
    return max(20, min(50, int(cfg.get("send_count", cfg.get("minimum_count", 20)) or 20)))


def _acg_dedupe_seconds(dispatcher):
    cfg = dispatcher.config.get("acg_images", {})
    days = max(1, min(90, int(cfg.get("dedupe_days", 7) or 7)))
    return days * 86400


def _prune_recent(state, dispatcher, now=None):
    now = time.time() if now is None else now
    cutoff = now - _acg_dedupe_seconds(dispatcher)
    state["recent"] = {
        url: timestamp for url, timestamp in state.get("recent", {}).items()
        if float(timestamp or 0) >= cutoff
    }


def _parse_clock(value):
    hour_text, minute_text = str(value).split(":", 1)
    return max(0, min(23, int(hour_text))), max(0, min(59, int(minute_text)))


def _schedule_windows(config, key, defaults):
    values = config.get(key)
    result = []
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                _parse_clock(item[0])
                _parse_clock(item[1])
            except (TypeError, ValueError):
                continue
            result.append((str(item[0]), str(item[1])))
    return result or list(defaults)


def _random_timestamp_for_window(day, window, timezone_name):
    zone = _timezone(timezone_name)
    start_hour, start_minute = _parse_clock(window[0])
    end_hour, end_minute = _parse_clock(window[1])
    start = datetime(day.year, day.month, day.day, start_hour, start_minute, 5, tzinfo=zone)
    end = datetime(day.year, day.month, day.day, end_hour, end_minute, 5, tzinfo=zone)
    span_minutes = max(0, int((end - start).total_seconds() // 60))
    return (start + timedelta(minutes=random.SystemRandom().randint(0, span_minutes))).timestamp()


def _ensure_daily_schedule(dispatcher, state, now=None):
    timezone_name = _scheduler_timezone(dispatcher)
    zone = _timezone(timezone_name)
    current = datetime.now(zone) if now is None else datetime.fromtimestamp(now, zone)
    date_key = current.strftime("%Y-%m-%d")
    schedule = state.get("schedule", {})
    if schedule.get("date") == date_key:
        return False
    acg_cfg = dispatcher.config.get("acg_images", {})
    hotboard_cfg = dispatcher.config.get("hotboard_push", {})
    acg_windows = _schedule_windows(acg_cfg, "windows", _DEFAULT_ACG_WINDOWS)
    hotboard_windows = _schedule_windows(hotboard_cfg, "windows", _DEFAULT_HOTBOARD_WINDOWS)
    acg_times = [_random_timestamp_for_window(current, window, timezone_name) for window in acg_windows]
    hotboard_times = [_random_timestamp_for_window(current, window, timezone_name) for window in hotboard_windows]
    current_timestamp = current.timestamp()
    state["schedule"] = {
        "date": date_key,
        "acg": acg_times,
        "hotboard": hotboard_times,
        "done": {
            "acg": [index for index, value in enumerate(acg_times) if value < current_timestamp - 60],
            "hotboard": [index for index, value in enumerate(hotboard_times) if value < current_timestamp - 60],
        },
    }
    log.info(
        "Daily content schedule generated: acg=%s hotboard=%s",
        [datetime.fromtimestamp(value, zone).strftime("%H:%M") for value in state["schedule"]["acg"]],
        [datetime.fromtimestamp(value, zone).strftime("%H:%M") for value in state["schedule"]["hotboard"]],
    )
    return True


async def _collect_one_acg_image(dispatcher):
    cfg = dispatcher.config.get("acg_images", {})
    if not cfg.get("enabled", True) or not _client_connected(dispatcher):
        return False
    async with _state_lock(dispatcher):
        state = _load_acg_state()
        _prune_recent(state, dispatcher)
        target = _acg_send_count(dispatcher)
        delivery_urls = (state.get("delivery") or {}).get("urls", [])
        if len(state["pool"]) >= target:
            _save_acg_state(state)
            return False
        seen = set(state["recent"]) | set(state["pool"]) | set(delivery_urls)
    from ..integrations.uapi import uapi_resolve_image_url
    url = await uapi_resolve_image_url(
        dispatcher, "/random/image", params={"category": "acg", "type": "pc"})
    if not url or url in seen:
        return False
    async with _state_lock(dispatcher):
        state = _load_acg_state()
        _prune_recent(state, dispatcher)
        delivery_urls = (state.get("delivery") or {}).get("urls", [])
        seen = set(state["recent"]) | set(state["pool"]) | set(delivery_urls)
        if url in seen:
            return False
        state["pool"].append(url)
        state["pool"] = state["pool"][-200:]
        _save_acg_state(state)
        log.debug("ACG pool filled: %d/%d", len(state["pool"]), _acg_send_count(dispatcher))
    return True


async def _batch_seen_in_history(dispatcher, group_id, batch_id):
    try:
        result = await dispatcher.client.get_group_msg_history(int(group_id), count=20)
        return batch_id in str(result)
    except Exception as error:
        log.info("ACG delivery history check failed group=%s batch=%s: %s", group_id, batch_id, error)
        return False


async def _notify_acg_terminal_failure(dispatcher, batch_id, groups, reason):
    owner_id = int(dispatcher.config.get("bot_owner") or 0)
    sender = getattr(dispatcher.client, "send_private_msg", None)
    if not owner_id or not callable(sender):
        return
    group_text = "、".join(str(group_id) for group_id in groups[:20]) or "未知群"
    message = "ACG 推送批次 {} 已停止重试。失败群：{}。原因：{}".format(
        batch_id, group_text, reason)
    try:
        await sender(owner_id, message[:1500])
    except Exception as error:
        log.warning("ACG terminal failure notification failed batch=%s: %s", batch_id, error)


async def _try_send_acg_delivery(dispatcher):
    terminal_notice = None
    async with _delivery_lock(dispatcher):
        now = time.time()
        cfg = dispatcher.config.get("acg_images", {})
        max_attempts = max(1, min(int(cfg.get("max_delivery_attempts", 3)), 10))
        retry_base = max(30, min(int(cfg.get("retry_base_seconds", 300)), 3600))
        retry_max = max(retry_base, min(int(cfg.get("retry_max_seconds", 1800)), 21600))
        delivery_ttl = max(300, min(int(cfg.get("delivery_ttl_seconds", 7200)), 86400))
        async with _state_lock(dispatcher):
            state = _load_acg_state()
            _prune_recent(state, dispatcher, now)
            delivery = state.get("delivery")
            if delivery:
                created_at = float(delivery.get("created_at", now) or now)
                if now - created_at >= delivery_ttl:
                    groups = [str(group_id) for group_id in delivery.get("remaining_groups", [])]
                    state["last_failure"] = {
                        "batch_id": str(delivery.get("batch_id", "")),
                        "groups": groups,
                        "reason": "delivery_expired",
                        "failed_at": now,
                    }
                    terminal_notice = (str(delivery.get("batch_id", "")), groups, "超过投递有效期")
                    state["delivery"] = None
                    state["pending_due"] = False
                    _save_acg_state(state)
                    delivery = None
            if terminal_notice is None and delivery and float(delivery.get("next_retry_at", 0) or 0) > now:
                _save_acg_state(state)
                return False
            if terminal_notice is None and delivery is None:
                target = _acg_send_count(dispatcher)
                if not state.get("pending_due") or len(state["pool"]) < target:
                    _save_acg_state(state)
                    return False
                urls = state["pool"][:target]
                state["pool"] = state["pool"][target:]
                groups = [str(gid) for gid in _enabled_group_ids(dispatcher)
                          if _feature_on(dispatcher, gid, "acg_images")]
                batch_id = datetime.now(_timezone(_scheduler_timezone(dispatcher))).strftime("%m%d-") + uuid.uuid4().hex[:6]
                delivery = {
                    "batch_id": batch_id,
                    "urls": urls,
                    "remaining_groups": groups,
                    "attempts": {},
                    "created_at": now,
                    "next_retry_at": 0,
                }
                state["delivery"] = delivery
                state["recent"].update({url: now for url in urls})
                _save_acg_state(state)
            if terminal_notice is None:
                delivery = dict(delivery)
        if terminal_notice is not None:
            await _notify_acg_terminal_failure(dispatcher, *terminal_notice)
            return False
        if not _client_connected(dispatcher):
            return False
        bot_qq = dispatcher.config.get("bot_qq", 0)
        remaining = list(delivery.get("remaining_groups", []))
        attempts = dict(delivery.get("attempts") or {})
        failed = []
        for gid in remaining:
            gid_text = str(gid)
            # A prior process may have stopped after OneBot accepted the forward
            # but before the delivery state was checkpointed. Recover from the
            # batch marker in group history instead of sending the batch again.
            if int(attempts.get(gid_text, 0) or 0) > 0:
                if await _batch_seen_in_history(dispatcher, gid_text, delivery["batch_id"]):
                    async with _state_lock(dispatcher):
                        state = _load_acg_state()
                        current = state.get("delivery")
                        if current and current.get("batch_id") == delivery.get("batch_id"):
                            current["remaining_groups"] = [
                                str(value) for value in current.get("remaining_groups", [])
                                if str(value) != gid_text
                            ]
                            current["attempts"] = attempts
                            state["delivery"] = current
                            _save_acg_state(state)
                    log.info("ACG package recovered from history group=%s batch=%s",
                             gid_text, delivery["batch_id"])
                    continue
            attempts[gid_text] = int(attempts.get(gid_text, 0) or 0) + 1
            # Persist the attempt before the external side effect. If the process
            # stops during the API call, the next process performs history recovery.
            async with _state_lock(dispatcher):
                state = _load_acg_state()
                current = state.get("delivery")
                if current and current.get("batch_id") == delivery.get("batch_id"):
                    current["attempts"] = attempts
                    state["delivery"] = current
                    _save_acg_state(state)
            header = "小汐的每日图片 · 批次 #{} · 共{}张".format(
                delivery["batch_id"], len(delivery["urls"]))
            nodes = [{
                "type": "node",
                "data": {"name": "小汐", "uin": str(bot_qq), "content": header},
            }] + [{
                "type": "node",
                "data": {"name": "小汐", "uin": str(bot_qq),
                         "content": [{"type": "image", "data": {"file": url}}]},
            } for url in delivery["urls"]]
            try:
                result = await dispatcher.client.send_group_forward_msg(int(gid), nodes)
                status = (result or {}).get("status") if isinstance(result, dict) else result
                confirmed = status == "ok"
                if not confirmed and status == "timeout":
                    confirmed = await _batch_seen_in_history(dispatcher, gid, delivery["batch_id"])
                if confirmed:
                    # Checkpoint each successful group immediately. Previously the
                    # state was updated only after every group completed, so a
                    # service restart replayed already delivered forwards.
                    async with _state_lock(dispatcher):
                        state = _load_acg_state()
                        current = state.get("delivery")
                        if current and current.get("batch_id") == delivery.get("batch_id"):
                            current["remaining_groups"] = [
                                str(value) for value in current.get("remaining_groups", [])
                                if str(value) != gid_text
                            ]
                            current["attempts"] = attempts
                            state["delivery"] = current
                            _save_acg_state(state)
                    log.info("ACG package sent group=%s batch=%s images=%d", gid, delivery["batch_id"], len(delivery["urls"]))
                else:
                    failed.append(str(gid))
                    log.warning("ACG package unconfirmed group=%s batch=%s status=%s attempt=%s/%s",
                                gid, delivery["batch_id"], status, attempts[str(gid)], max_attempts)
            except Exception as error:
                failed.append(str(gid))
                log.warning("ACG package failed group=%s batch=%s attempt=%s/%s: %s",
                            gid, delivery["batch_id"], attempts[str(gid)], max_attempts, error)
            await asyncio.sleep(2)
        exhausted = [gid for gid in failed if attempts.get(str(gid), 0) >= max_attempts]
        retryable = [gid for gid in failed if gid not in exhausted]
        async with _state_lock(dispatcher):
            state = _load_acg_state()
            current = state.get("delivery")
            if current and current.get("batch_id") == delivery.get("batch_id"):
                current["attempts"] = attempts
                if retryable:
                    highest_attempt = max(attempts.get(str(gid), 1) for gid in retryable)
                    retry_seconds = min(retry_max, retry_base * (2 ** max(0, highest_attempt - 1)))
                    current["remaining_groups"] = retryable
                    current["next_retry_at"] = time.time() + retry_seconds
                    state["delivery"] = current
                else:
                    state["delivery"] = None
                    state["pending_due"] = False
                if exhausted:
                    state["last_failure"] = {
                        "batch_id": str(delivery.get("batch_id", "")),
                        "groups": exhausted,
                        "reason": "attempts_exhausted",
                        "attempts": {gid: attempts.get(gid, 0) for gid in exhausted},
                        "failed_at": time.time(),
                    }
                _save_acg_state(state)
        if exhausted:
            await _notify_acg_terminal_failure(
                dispatcher, str(delivery.get("batch_id", "")), exhausted,
                "达到最大重试次数 {}".format(max_attempts))
        return not failed


async def _daily_acg_push(dispatcher):
    """Persist an ACG delivery request for the collector to fulfill."""
    cfg = dispatcher.config.get("acg_images", {})
    if not cfg.get("enabled", False):
        return True
    async with _state_lock(dispatcher):
        state = _load_acg_state()
        state["pending_due"] = True
        _save_acg_state(state)
    await _try_send_acg_delivery(dispatcher)
    return True


async def _acg_collector_loop(dispatcher):
    try:
        while dispatcher.client._running:
            cfg = dispatcher.config.get("acg_images", {})
            if not cfg.get("enabled", True):
                await asyncio.sleep(30)
                continue
            try:
                await _collect_one_acg_image(dispatcher)
                await _try_send_acg_delivery(dispatcher)
            except Exception as error:
                log.warning("ACG collector iteration failed: %s", error)
                await asyncio.sleep(15)
                continue
            interval = max(1, min(30, int(cfg.get("collector_interval_seconds", 1) or 1)))
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


async def _daily_hotboard_push(dispatcher):
    """Fetch configured hot boards and confirm every group delivery."""
    if not _client_connected(dispatcher):
        log.info("Hotboard push deferred: OneBot is offline")
        return False
    cfg = dispatcher.config.get("hotboard_push", {})
    if not cfg.get("enabled", False):
        return True
    boards = cfg.get("types", ["weibo"]) or ["weibo"]
    groups = [gid for gid in _enabled_group_ids(dispatcher)
              if _feature_on(dispatcher, gid, "hotboard_push")]
    if not groups:
        return True
    from ..integrations import uapi as _uapi
    all_delivered = True
    for board in boards:
        board = str(board)[:20]
        if not _uapi.credits_available(
                dispatcher.config, "auto", path="/misc/hotboard"):
            log.info("hotboard push deferred: auto credit budget exhausted")
            return False
        data = await _uapi.uapi_get(dispatcher, "/misc/hotboard",
                                    params={"type": board}, kind="auto")
        items = (data or {}).get("list") if isinstance(data, dict) else None
        if not items:
            log.info("hotboard push deferred: board=%s no data", board)
            all_delivered = False
            continue
        digest = await build_detailed_hotboard(dispatcher, board, items)
        nodes = build_hotboard_forward_nodes(
            board, digest["items"], dispatcher.config.get("bot_qq", 0),
            limit=len(digest["items"]), summary=digest["summary"], details=digest["details"])
        for gid in groups:
            try:
                result = await dispatcher.client.send_group_forward_msg(int(gid), nodes)
                status = ((result or {}).get("status")
                          if isinstance(result, dict) else result)
                if status != "ok":
                    all_delivered = False
                    log.warning("hotboard push failed group=%s board=%s status=%s",
                                gid, board, status)
                else:
                    log.info("hotboard push: group=%s board=%s items=%d status=ok",
                             gid, board, len(nodes) - 1)
            except Exception as error:
                all_delivered = False
                log.warning("hotboard push failed group=%s board=%s: %s", gid, board, error)
            await asyncio.sleep(2)
    return all_delivered

def _scheduled_jobs(dispatcher):
    """Return randomized content jobs plus the fixed daily check-in."""
    timezone_name = _scheduler_timezone(dispatcher)
    now = time.time()
    state = _load_acg_state()
    if _ensure_daily_schedule(dispatcher, state, now):
        _save_acg_state(state)
    jobs = [("checkin", _seconds_until_next_checkin(timezone_name), None)]
    schedule = state["schedule"]
    done = schedule.get("done", {})
    if dispatcher.config.get("acg_images", {}).get("enabled", True):
        for index, timestamp in enumerate(schedule.get("acg", [])):
            if index not in done.get("acg", []):
                jobs.append(("acg", max(0, float(timestamp) - now), index))
    if dispatcher.config.get("hotboard_push", {}).get("enabled", True):
        for index, timestamp in enumerate(schedule.get("hotboard", [])):
            if index not in done.get("hotboard", []):
                jobs.append(("hotboard", max(0, float(timestamp) - now), index))
    return jobs


def _mark_scheduled_job_done(dispatcher, name, index):
    if index is None:
        return
    state = _load_acg_state()
    if _ensure_daily_schedule(dispatcher, state):
        _save_acg_state(state)
        return
    done = state["schedule"].setdefault("done", {}).setdefault(name, [])
    if index not in done:
        done.append(index)
    _save_acg_state(state)


async def _execute_scheduled_job(dispatcher, name):
    if name == "checkin":
        if not _client_connected(dispatcher):
            log.info("Scheduled check-in skipped: OneBot is offline")
            return True
        await _daily_checkin(dispatcher)
        return True
    if name == "acg":
        return await _daily_acg_push(dispatcher)
    if name == "hotboard":
        return await _daily_hotboard_push(dispatcher)
    raise ValueError("unknown scheduled job: {}".format(name))


async def _run_due_scheduled_job(dispatcher, name, index):
    completed = await _execute_scheduled_job(dispatcher, name)
    if name != "checkin" and completed:
        _mark_scheduled_job_done(dispatcher, name, index)
    return completed

async def scheduler_loop(dispatcher):
    """Run check-in, randomized content jobs, and the ACG pool collector."""
    log.info("Scheduler started")
    collector = asyncio.create_task(_acg_collector_loop(dispatcher))
    try:
        while dispatcher.client._running:
            jobs = _scheduled_jobs(dispatcher)
            name, wait_seconds, index = min(jobs, key=lambda item: item[1])
            if wait_seconds <= 60:
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                completed = True
                try:
                    completed = await _run_due_scheduled_job(dispatcher, name, index)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    completed = False
                    log.warning("Scheduled job %s failed: %s", name, error, exc_info=True)
                await asyncio.sleep(65 if completed else 300)
            else:
                chunk = min(1800, wait_seconds - 30)
                await asyncio.sleep(chunk if chunk > 0 else 60)
    except asyncio.CancelledError:
        pass
    finally:
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
    log.info("Scheduler stopped")
