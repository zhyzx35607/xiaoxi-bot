"""bot/scheduler.py - Scheduled tasks (morning/evening greetings, cleanup).

Currently lightweight by design — on low-resource servers, avoid heavy
periodic work. Only started when config.runtime.enable_scheduler is true.
"""

import asyncio
import logging

log = logging.getLogger("qqbot")


async def scheduler_loop(dispatcher):
    """Main scheduler loop. Add timed tasks inside as needed."""
    log.info("Scheduler started")
    try:
        while dispatcher.client._running:
            # Placeholder: add daily-reset / greeting tasks here.
            # Keep interval long to minimise wake-ups on low-spec hosts.
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    log.info("Scheduler stopped")
