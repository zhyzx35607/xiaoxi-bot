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
from datetime import datetime, timedelta
from .utils import atomic_write_json

log = logging.getLogger("qqbot")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHECKIN_STATUS_PATH = os.path.join(_ROOT, "data", "checkin_status.json")


def _seconds_until_next_checkin():
    """Calculate seconds until the next 00:00:01 local check-in."""
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
    state["next_run"] = time.time() + _seconds_until_next_checkin()
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


def _seconds_until_time(hour, minute=0):
    """Seconds until the next local HH:MM:05 occurrence."""
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


def format_hotboard(board, items, limit=10):
    """Format hot-board items into a compact group message."""
    name = BOARD_NAMES.get(board, board)
    lines = ["【{}热榜】".format(name)]
    for index, item in enumerate(items[:limit], 1):
        title = str(item.get("title") or "").strip()[:40]
        hot = str(item.get("hot_value") or "").strip()
        line = "{}. {}".format(index, title)
        if hot:
            line += "（{}）".format(hot)
        lines.append(line)
    return "\n".join(lines)


def _feature_on(dispatcher, gid, name):
    from .permission import get_group_config
    gcfg = get_group_config(dispatcher, gid)
    return gcfg.get("features", {}).get(name, True)


async def _daily_acg_push(dispatcher):
    """Push random ACG images at 0/6/12/18. URLs are resolved once and
    reused for every group; NapCat fetches them directly (no bot memory)."""
    cfg = dispatcher.config.get("acg_images", {})
    if not cfg.get("enabled", True):
        return
    lo, hi = int(cfg.get("min", 10) or 10), int(cfg.get("max", 20) or 20)
    count = random.randint(min(lo, hi), max(lo, hi))
    from .uapi import uapi_resolve_image_url
    urls = []
    for _ in range(count):
        url = await uapi_resolve_image_url(
            dispatcher, "/random/image", params={"category": "acg", "type": "pc"})
        if url:
            urls.append(url)
        await asyncio.sleep(0.3)
    if not urls:
        log.info("ACG push skipped: no image urls resolved")
        return
    groups = [gid for gid in _enabled_group_ids(dispatcher)
              if _feature_on(dispatcher, gid, "acg_images")]
    for gid in groups:
        try:
            if len(urls) <= 15:
                for i in range(0, len(urls), 5):
                    segments = [{"type": "image", "data": {"file": u}}
                                for u in urls[i:i + 5]]
                    await dispatcher.client.send_group_msg(int(gid), segments)
                    await asyncio.sleep(2)
            else:
                bot_qq = dispatcher.config.get("bot_qq", 0)
                nodes = [{
                    "type": "node",
                    "data": {"name": "小汐", "uin": str(bot_qq),
                             "content": [{"type": "image", "data": {"file": u}}]},
                } for u in urls]
                await dispatcher.client.send_group_forward_msg(int(gid), nodes)
            log.info("ACG push: group=%s images=%d", gid, len(urls))
        except Exception as e:
            log.warning("ACG push failed group=%s: %s", gid, e)
        await asyncio.sleep(2)


async def _daily_hotboard_push(dispatcher):
    """Push hot boards at 9/21. Fetched once per board, broadcast to all
    enabled groups (saves credits)."""
    cfg = dispatcher.config.get("hotboard_push", {})
    if not cfg.get("enabled", True):
        return
    boards = cfg.get("types", ["bilibili"]) or ["bilibili"]
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
        text = format_hotboard(board, items)
        for gid in groups:
            try:
                await dispatcher.client.send_group_msg(
                    int(gid), [{"type": "text", "data": {"text": text}}])
            except Exception as e:
                log.warning("hotboard push failed group=%s: %s", gid, e)
            await asyncio.sleep(2)


def _scheduled_jobs(dispatcher):
    """Return [(job_name, seconds_until_fire), ...] for all timed jobs."""
    jobs = [("checkin", _seconds_until_next_checkin())]
    acg_cfg = dispatcher.config.get("acg_images", {})
    if acg_cfg.get("enabled", True):
        for hour in acg_cfg.get("times", [0, 6, 12, 18]):
            jobs.append(("acg", _seconds_until_time(hour)))
    hb_cfg = dispatcher.config.get("hotboard_push", {})
    if hb_cfg.get("enabled", True):
        for hour in hb_cfg.get("times", [9, 21]):
            jobs.append(("hotboard", _seconds_until_time(hour)))
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
                if name == "checkin":
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
