"""Application startup and shutdown orchestration."""

import asyncio
import logging
import os
import signal

from bot.client import OneBotClient
from bot.commands import register_all
from bot.dispatcher import Dispatcher
from bot.utils import atomic_write_json
from .config import apply_env_overrides, load_config, migrate_config

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("qqbot")

async def amain():
    config_path = os.getenv("QQBOT_CONFIG_PATH") or os.path.join(_BASE_DIR, "config.json")
    config = load_config(config_path)

    # Migrate old config if needed
    config, migrated = migrate_config(config)
    if migrated:
        atomic_write_json(config_path, config, indent=2)
        atomic_write_json(config_path + ".last-good", config, indent=2)
        log.info("Config migrated to new format")
    config = apply_env_overrides(config)

    log.info("Bot %s starting...", config["bot_qq"])
    enabled_groups = [
        group_id for group_id, group_cfg in config.get("groups", {}).items()
        if isinstance(group_cfg, dict) and group_cfg.get("enabled") is True
    ]
    log.info("Enabled groups: %s", enabled_groups)

    client = OneBotClient(config)
    dispatcher = Dispatcher(config, client, config_path)
    client.set_dispatcher(dispatcher)
    register_all(dispatcher)
    log.info("Registered %d commands. Ready.", len(dispatcher.commands))

    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Received shutdown signal")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    client_task = asyncio.create_task(client.run())
    await asyncio.sleep(2)
    if client_task.done():
        log.warning("Client task exited during startup; stopping main loop")
        return
    dispatcher.start_delayed_worker()
    dispatcher.start_scheduler()
    dispatcher.start_bili_push()
    dispatcher.start_rss_guard()

    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {stop_task, client_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        if task is stop_task:
            task.cancel()
    if client_task in done:
        log.warning("Client task exited; stopping bot")
    dispatcher.save_runtime_state(force=True)
    await dispatcher.stop_delayed_worker()
    await dispatcher.stop_scheduler()
    await dispatcher.stop_bili_push()
    await dispatcher.stop_rss_guard()
    await client.stop()
    await dispatcher.stop_background_tasks()
    try:
        await asyncio.wait_for(client_task, timeout=15)
    except asyncio.TimeoutError:
        client_task.cancel()
        try:
            await asyncio.wait_for(client_task, timeout=5)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            log.warning("Client task did not exit after cancellation")
    log.info("Bot stopped")
