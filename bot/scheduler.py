"""bot/scheduler.py - Scheduled tasks (morning/evening greetings, cleanup, check-in).

Currently lightweight by design — on low-resource servers, avoid heavy
periodic work. Only started when config.runtime.enable_scheduler is true.
"""

import asyncio
import logging
import time
from datetime import datetime

log = logging.getLogger("qqbot")


def _seconds_until_next_midnight():
    """Calculate seconds until next 00:00:00 local time."""
    now = datetime.now()
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = tomorrow.replace(day=now.day + 1)
    return (tomorrow - now).total_seconds()


async def _daily_checkin(dispatcher):
    """Silent daily check-in at 00:00:00 for all enabled groups."""
    try:
        enabled = set(dispatcher.config.get("groups", {}).keys())
        # Also check in group_defaults if no per-group config
        group_list = []

        # Get all groups the bot is in
        try:
            resp = await dispatcher.client.get_group_list()
            if resp and resp.get("status") == "ok" and isinstance(resp.get("data"), list):
                for g in resp["data"]:
                    gid = str(g.get("group_id", ""))
                    if gid:
                        group_list.append(gid)
        except Exception:
            pass

        if not group_list:
            return

        for gid in group_list:
            # Only check in for enabled groups
            if enabled and gid not in enabled:
                continue
            # Check per-group disable flag
            group_cfg = dispatcher.config.get("groups", {}).get(gid, {})
            if group_cfg.get("disabled"):
                continue
            try:
                await dispatcher.client.send_group_sign(int(gid))
                log.debug("Daily check-in: group %s", gid)
            except Exception as e:
                log.debug("Daily check-in failed for group %s: %s", gid, e)
            await asyncio.sleep(2)  # Small gap between groups
    except Exception as e:
        log.warning("Daily check-in error: %s", e)


async def scheduler_loop(dispatcher):
    """Main scheduler loop. Handles daily check-in and cleanup tasks."""
    log.info("Scheduler started")
    try:
        while dispatcher.client._running:
            # === Daily check-in at 00:00 ===
            wait_seconds = _seconds_until_next_midnight()
            # If extremely close to midnight (< 1 min away), wait exactly for it
            if wait_seconds <= 60:
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                await _daily_checkin(dispatcher)
                # Sleep 65 seconds past midnight to avoid double-firing
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
