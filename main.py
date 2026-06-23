# main.py - QQ Bot entry point
import asyncio, json, logging, os, sys, signal
from logging.handlers import RotatingFileHandler

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE_DIR)

from bot.client import OneBotClient
from bot.dispatcher import Dispatcher
from bot.commands import register_all
from bot.utils import atomic_write_json

_handlers = [
    RotatingFileHandler(
        os.path.join(_BASE_DIR, "bot.log"),
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
]
if os.getenv("QQBOT_CONSOLE_LOG", "").lower() in {"1", "true", "yes", "on"}:
    _handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_handlers,
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("qqbot")


def apply_env_overrides(config):
    """Load secrets/runtime endpoints from environment without writing them to config.json."""
    env_map = {
        "QQBOT_WS_URL": "ws_url",
        "QQBOT_TOKEN": "token",
        "QQBOT_ONEBOT_TOKEN": "token",
        "ONEBOT_ACCESS_TOKEN": "token",
        "DEEPSEEK_API_KEY": "deepseek_api_key",
        "QQBOT_DEEPSEEK_API_KEY": "deepseek_api_key",
        "DEEPSEEK_BASE_URL": "deepseek_base_url",
        "QQBOT_DEEPSEEK_BASE_URL": "deepseek_base_url",
        "DEEPSEEK_MODEL": "deepseek_model",
        "QQBOT_DEEPSEEK_MODEL": "deepseek_model",
    }
    for env_name, cfg_key in env_map.items():
        value = os.getenv(env_name)
        if value:
            config[cfg_key] = value

    vision_key = os.getenv("VISION_API_KEY") or os.getenv("QQBOT_VISION_API_KEY")
    if vision_key:
        config.setdefault("vision_api", {})["api_key"] = vision_key

    vision_base = os.getenv("VISION_API_BASE_URL") or os.getenv("QQBOT_VISION_API_BASE_URL")
    if vision_base:
        config.setdefault("vision_api", {})["base_url"] = vision_base

    vision_model = os.getenv("VISION_API_MODEL") or os.getenv("QQBOT_VISION_API_MODEL")
    if vision_model:
        config.setdefault("vision_api", {})["model"] = vision_model

    return config


def migrate_config(config):
    """Migrate old config format to new group-based format."""
    migrated = False

    # Ensure group_defaults exists
    if "group_defaults" not in config:
        config["group_defaults"] = {
            "welcome_msg": config.pop("welcome_msg", {"enabled": True, "template": "哟 {nickname} 来了"}),
            "bad_words": config.pop("bad_words", {"enabled": True, "auto_delete": True, "warn_msg": "@{user} 注意一下发言", "words": []}),
            "features": {
                "ai_chat": True, "interject": True, "repeat": True, "music": True,
                "fortune": True, "admin_cmds": True, "voice_reply": False,
                "auto_poke": True, "auto_essence": False
            }
        }
        migrated = True

    # Ensure groups exists and migrate enabled_groups
    if "groups" not in config:
        config["groups"] = {}
        migrated = True

    if "enabled_groups" in config:
        for gid in config["enabled_groups"]:
            gid_str = str(gid)
            if gid_str not in config["groups"]:
                config["groups"][gid_str] = {
                    "enabled": True,
                    "masters": [],
                    "welcome_msg": dict(config["group_defaults"]["welcome_msg"]),
                    "bad_words": dict(config["group_defaults"]["bad_words"]),
                    "features": dict(config["group_defaults"]["features"]),
                }
        del config["enabled_groups"]
        migrated = True


    runtime_defaults = {
        "ws_queue_size": 50,
        "max_event_tasks": 3,
        "max_background_tasks": 6,
        "api_timeout_seconds": 6,
        "connect_timeout_seconds": 5,
        "reconnect_max_delay_seconds": 60,
        "ai_concurrency": 1,
        "search_concurrency": 1,
        "vision_concurrency": 1,
        "non_explicit_judge_cooldown": 180,
        "enable_long_memory_compress": False,
        "enable_scheduler": False,
    }
    runtime = config.setdefault("runtime", {})
    for key, value in runtime_defaults.items():
        if key not in runtime:
            runtime[key] = value
            migrated = True

    sticker_mode = config.setdefault("sticker_mode", {})
    for key, value in {"vision_analyze": False, "smart_pick": False}.items():
        if key not in sticker_mode:
            sticker_mode[key] = value
            migrated = True

    natural_chat_defaults = {
        "interject_threshold": 68,
        "followup_threshold": 42,
        "interject_min_probability": 0.08,
        "interject_max_probability": 0.62,
        "followup_probability": 0.85,
        "quiet_after_reply_seconds": 75,
    }
    natural_chat = config.setdefault("natural_chat", {})
    for key, value in natural_chat_defaults.items():
        if key not in natural_chat:
            natural_chat[key] = value
            migrated = True

    security_defaults = {
        "url_check_enabled": True,
        "gray_tip_protect_enabled": True,
        "auto_punish": True,
        "ban_seconds": 600,
        "max_log_entries": 200,
    }
    security = config.setdefault("security", {})
    for key, value in security_defaults.items():
        if key not in security:
            security[key] = value
            migrated = True

    return config, migrated


async def amain():
    config_path = os.path.join(_BASE_DIR, "config.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # Migrate old config if needed
    config, migrated = migrate_config(config)
    if migrated:
        atomic_write_json(config_path, config, indent=2)
        log.info("Config migrated to new format")
    config = apply_env_overrides(config)

    log.info("Bot %s starting...", config["bot_qq"])
    log.info("Groups: %s", list(config.get("groups", {}).keys()))

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
    dispatcher.start_scheduler()

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
    await dispatcher.stop_scheduler()
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


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as e:
        log.exception("Fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
