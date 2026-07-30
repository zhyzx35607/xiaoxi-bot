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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_TZ_WARNING_NAMES = set()
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
                    log.warning("Group check-in failed: group %s trigger=%s retcode=%s message=%s",
                                gid, trigger, result.get("retcode"),
                                str(result.get("message") or result.get("msg", ""))[:120])
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


def format_hotboard(board, items, limit=10, summary=None):
    """Format hot-board items into a group message with clickable links."""
    name = BOARD_NAMES.get(board, board)
    lines = ["【{}热榜】".format(name)]
    if summary:
        lines.append(summary)
    for index, item in enumerate(items[:limit], 1):
        title = str(item.get("title") or "").strip()[:40]
        line = "{}. {}".format(index, title)
        hot = str(item.get("hot_value") or "").strip()
        if hot:
            line += "（{}）".format(hot)
        lines.append(line)
        url = str(item.get("url") or "").strip()
        if url:
            lines.append(url)
    return "\n".join(lines)


def build_hotboard_forward_nodes(board, items, bot_qq, limit=10, summary=None):
    """Build a compact merged-forward message for one hot board."""
    name = BOARD_NAMES.get(board, board)
    header = "【{}热榜】".format(name)
    if summary:
        header += "\n" + str(summary).strip()
    node_name = "小汐"
    node_uin = str(bot_qq)
    nodes = [{
        "type": "node",
        "data": {"name": node_name, "uin": node_uin, "content": header},
    }]
    for index, item in enumerate(items[:limit], 1):
        title = str(item.get("title") or "").strip()[:80] or "（无标题）"
        text = "{}. {}".format(index, title)
        hot = str(item.get("hot_value") or "").strip()
        if hot:
            text += "（{}）".format(hot)
        url = str(item.get("url") or "").strip()
        if url:
            text += "\n" + url
        nodes.append({
            "type": "node",
            "data": {"name": node_name, "uin": node_uin, "content": text},
        })
    return nodes


async def ai_hotboard_summary(dispatcher, board, items):
    """One-line AI overview of a hot board. None on any failure."""
    try:
        titles = [str(i.get("title") or "").strip()
                  for i in items[:15] if i.get("title")]
        if not titles:
            return None
        from .ai import _call_deepseek
        name = BOARD_NAMES.get(board, board)
        messages = [
            {"role": "system",
             "content": "你是新闻摘要助手。根据热榜标题，用一两句口语化中文概括当前热点趋势，60字以内，不逐条复述，不加表情。"},
            {"role": "user",
             "content": name + "热榜：\n" + "\n".join(titles)},
        ]
        client = getattr(dispatcher, "client", None)
        session = getattr(client, "session", None)
        text = await _call_deepseek(dispatcher.config, messages,
                                    max_tokens=80, temperature=0.3,
                                    session=session)
        text = str(text or "").strip().replace("\n", " ")
        return text[:120] or None
    except Exception as e:
        log.info("hotboard AI summary failed: %s", e)
        return None


def _feature_on(dispatcher, gid, name):
    from .permission import get_group_config
    gcfg = get_group_config(dispatcher, gid)
    return gcfg.get("features", {}).get(name, True)


_ACG_HISTORY_PATH = os.path.join(_ROOT, "data", "acg_history.json")


def _load_acg_state():
    try:
        with open(_ACG_HISTORY_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        urls = data.get("urls") if isinstance(data, dict) else None
        pending = data.get("pending") if isinstance(data, dict) else None
        clean_urls = ([str(u) for u in urls if isinstance(u, str)][-3000:]
                      if isinstance(urls, list) else [])
        clean_pending = {}
        if isinstance(pending, dict):
            for gid, values in pending.items():
                if isinstance(values, list):
                    clean_pending[str(gid)] = [
                        str(value) for value in values if isinstance(value, str)
                    ][-200:]
        return {"urls": clean_urls, "pending": clean_pending}
    except Exception:
        pass
    return {"urls": [], "pending": {}}


def _load_acg_history():
    return _load_acg_state()["urls"]


def _save_acg_history(urls, pending=None):
    try:
        if pending is None:
            pending = _load_acg_state()["pending"]
        atomic_write_json(_ACG_HISTORY_PATH, {
            "urls": urls[-3000:],
            "pending": pending,
        })
    except Exception as e:
        log.warning("ACG history save failed: %s", e)


async def _daily_acg_push(dispatcher):
    """Push random ACG images at 0/6/12/18 as one merged-forward message.

    The upstream API has no date filter, so freshness is approximated by
    remembering every URL ever sent and skipping repeats. URLs are resolved
    once and reused for every group; NapCat fetches them directly."""
    cfg = dispatcher.config.get("acg_images", {})
    if not cfg.get("enabled", True):
        return
    count = max(1, min(100, int(cfg.get("count", 50) or 50)))
    batch_size = max(1, min(20, int(cfg.get("batch_size", 10) or 10)))
    groups = [gid for gid in _enabled_group_ids(dispatcher)
              if _feature_on(dispatcher, gid, "acg_images")]
    if not groups:
        return
    if not _client_connected(dispatcher):
        log.info("ACG push skipped: OneBot is offline")
        return
    from .uapi import uapi_resolve_image_url
    state = _load_acg_state()
    history = state["urls"]
    pending = state["pending"]
    seen = set(history)
    for values in pending.values():
        seen.update(values)
    urls = []
    attempts = 0
    has_uapi_key = bool(str(dispatcher.config.get("uapi_api_key") or "").strip())
    if has_uapi_key:
        while len(urls) < count and attempts < count * 2:
            attempts += 1
            url = await uapi_resolve_image_url(
                dispatcher, "/random/image", params={"category": "acg", "type": "pc"})
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
            await asyncio.sleep(0.3)
    elif not any(pending.get(str(gid)) for gid in groups):
        log.info("ACG push skipped: uapi api key is not configured")
        return
    if not urls and not any(pending.get(str(gid)) for gid in groups):
        log.info("ACG push skipped: no image urls resolved")
        return
    bot_qq = dispatcher.config.get("bot_qq", 0)
    for gid in groups:
        gid_key = str(gid)
        group_urls = list(dict.fromkeys((pending.get(gid_key) or []) + urls))
        unsent = []
        for start in range(0, len(group_urls), batch_size):
            batch = group_urls[start:start + batch_size]
            nodes = [{
                "type": "node",
                "data": {"name": "小汐", "uin": str(bot_qq),
                         "content": [{"type": "image", "data": {"file": url}}]},
            } for url in batch]
            try:
                result = await dispatcher.client.send_group_forward_msg(int(gid), nodes)
                status = ((result or {}).get("status")
                          if isinstance(result, dict) else result)
                if status != "ok":
                    unsent.extend(group_urls[start:])
                    log.warning(
                        "ACG batch unconfirmed group=%s batch=%d images=%d status=%s",
                        gid, start // batch_size + 1, len(batch), status)
                    break
                log.info("ACG batch sent group=%s batch=%d images=%d",
                         gid, start // batch_size + 1, len(batch))
            except Exception as e:
                unsent.extend(group_urls[start:])
                log.warning("ACG batch failed group=%s batch=%d: %s",
                            gid, start // batch_size + 1, e)
                break
            await asyncio.sleep(2)
        if unsent:
            pending[gid_key] = list(dict.fromkeys(unsent))[-200:]
        else:
            pending.pop(gid_key, None)
        await asyncio.sleep(2)
    history.extend(urls)
    _save_acg_history(history, pending)


async def _daily_hotboard_push(dispatcher):
    """Push hot boards at 9/21. Fetched once per board, broadcast to all
    enabled groups (saves credits)."""
    if not _client_connected(dispatcher):
        log.info("Hotboard push skipped: OneBot is offline")
        return
    if not str(dispatcher.config.get("uapi_api_key") or "").strip():
        log.info("Hotboard push skipped: uapi api key is not configured")
        return
    cfg = dispatcher.config.get("hotboard_push", {})
    if not cfg.get("enabled", True):
        return
    boards = cfg.get("types", ["weibo"]) or ["weibo"]
    groups = [gid for gid in _enabled_group_ids(dispatcher)
              if _feature_on(dispatcher, gid, "hotboard_push")]
    if not groups:
        return
    from . import uapi as _uapi
    for board in boards:
        board = str(board)[:20]
        if not _uapi.credits_available(dispatcher.config, "auto"):
            log.info("hotboard push skipped: auto credit budget exhausted")
            break
        data = await _uapi.uapi_get(dispatcher, "/misc/hotboard",
                                    params={"type": board}, kind="auto")
        items = (data or {}).get("list") if isinstance(data, dict) else None
        if not items:
            log.info("hotboard push: board=%s no data", board)
            continue
        summary = await ai_hotboard_summary(dispatcher, board, items)
        nodes = build_hotboard_forward_nodes(
            board, items, dispatcher.config.get("bot_qq", 0), summary=summary)
        for gid in groups:
            try:
                result = await dispatcher.client.send_group_forward_msg(int(gid), nodes)
                status = ((result or {}).get("status")
                          if isinstance(result, dict) else result)
                if status != "ok":
                    log.warning("hotboard push failed group=%s board=%s status=%s",
                                gid, board, status)
                else:
                    log.info("hotboard push: group=%s board=%s items=%d status=ok",
                             gid, board, len(nodes) - 1)
            except Exception as e:
                log.warning("hotboard push failed group=%s board=%s: %s",
                            gid, board, e)
            await asyncio.sleep(2)


def _scheduled_jobs(dispatcher):
    """Return [(job_name, seconds_until_fire), ...] for all timed jobs."""
    timezone_name = _scheduler_timezone(dispatcher)
    jobs = [("checkin", _seconds_until_next_checkin(timezone_name))]
    acg_cfg = dispatcher.config.get("acg_images", {})
    if acg_cfg.get("enabled", True):
        for hour in acg_cfg.get("times", [0, 6, 12, 18]):
            jobs.append(("acg", _seconds_until_time(
                hour, timezone_name=timezone_name)))
    hb_cfg = dispatcher.config.get("hotboard_push", {})
    if hb_cfg.get("enabled", True):
        for hour in hb_cfg.get("times", [9, 21]):
            jobs.append(("hotboard", _seconds_until_time(
                hour, timezone_name=timezone_name)))
    return jobs


async def scheduler_loop(dispatcher):
    """Main scheduler loop. Handles daily check-in and cleanup tasks."""
    log.info("Scheduler started")
    try:
        while dispatcher.client._running:
            jobs = _scheduled_jobs(dispatcher)
            name, wait_seconds = min(jobs, key=lambda item: item[1])
            if wait_seconds <= 60:
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                if not _client_connected(dispatcher):
                    log.info("Scheduled job %s skipped: OneBot is offline", name)
                elif name == "checkin":
                    await _daily_checkin(dispatcher)
                elif name == "acg":
                    await _daily_acg_push(dispatcher)
                elif name == "hotboard":
                    await _daily_hotboard_push(dispatcher)
                # Sleep past the fire window to avoid double-firing
                await asyncio.sleep(65)
            else:
                # Sleep in 30-minute chunks to stay responsive to shutdown
                chunk = min(1800, wait_seconds - 30)  # leave 30s buffer
                if chunk > 0:
                    await asyncio.sleep(chunk)
                else:
                    await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    log.info("Scheduler stopped")
