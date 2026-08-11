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


async def _wait_for_startup_connection(client, client_task, timeout):
    """Wait for readiness or a client exit without delaying shutdown unnecessarily."""
    if client.is_connected:
        return True
    wait_task = asyncio.create_task(client.wait_until_connected(timeout=timeout))
    done, _ = await asyncio.wait(
        {wait_task, client_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    if wait_task in done:
        return bool(wait_task.result())
    wait_task.cancel()
    try:
        await wait_task
    except asyncio.CancelledError:
        pass
    return bool(client.is_connected)


async def _shutdown_runtime(dispatcher, client, client_task, stop_task=None):
    """Stop every runtime component and surface client task failures."""
    if stop_task is not None and not stop_task.done():
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
    dispatcher.save_runtime_state(force=True)
    for name, stop in (
            ("delayed worker", dispatcher.stop_delayed_worker),
            ("scheduler", dispatcher.stop_scheduler),
            ("Bilibili push", dispatcher.stop_bili_push),
            ("RSS guard", dispatcher.stop_rss_guard),
            ("Agent worker", dispatcher.agent_worker.stop)):
        try:
            await stop()
        except Exception:
            log.exception("Failed to stop %s", name)
    try:
        await client.stop()
    except Exception:
        log.exception("Failed to stop OneBot client")
    try:
        await dispatcher.stop_background_tasks()
    except Exception:
        log.exception("Failed to stop dispatcher background tasks")

    if client_task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(client_task), timeout=15)
    except asyncio.TimeoutError:
        client_task.cancel()
        try:
            await asyncio.wait_for(client_task, timeout=5)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            log.warning("Client task did not exit after cancellation")

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

    client_task = stop_task = None
    try:
        client_task = asyncio.create_task(client.run())
        await asyncio.sleep(0)
        if client_task.done():
            log.warning("Client task exited during startup")
            await client_task
        startup_timeout = config.get("runtime", {}).get("startup_connect_timeout_seconds", 30)
        try:
            startup_timeout = max(1.0, float(startup_timeout))
        except (TypeError, ValueError):
            startup_timeout = 30.0
        connected = await _wait_for_startup_connection(client, client_task, startup_timeout)
        if client_task.done():
            log.warning("Client task exited during startup")
            await client_task
        if connected:
            log.info("OneBot connection ready; starting background workers")
        else:
            log.warning(
                "OneBot connection not ready after %.0fs; starting background workers while reconnecting",
                startup_timeout,
            )
        dispatcher.start_delayed_worker()
        dispatcher.start_scheduler()
        dispatcher.start_bili_push()
        dispatcher.start_rss_guard()
        dispatcher.agent_worker.start()

        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {stop_task, client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if client_task in done:
            log.warning("Client task exited; stopping bot")
            await client_task
    finally:
        await _shutdown_runtime(dispatcher, client, client_task, stop_task)
    log.info("Bot stopped")
