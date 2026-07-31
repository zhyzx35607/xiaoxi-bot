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
chat_log = logging.getLogger("qqbot.chat")
chat_log.setLevel(logging.INFO)
chat_log.propagate = False
chat_handler = RotatingFileHandler(
    os.path.join(_BASE_DIR, "chat.log"),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
chat_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
chat_log.addHandler(chat_handler)

for _log_path in (os.path.join(_BASE_DIR, "bot.log"),
                  os.path.join(_BASE_DIR, "chat.log")):
    try:
        os.chmod(_log_path, 0o600)
    except OSError:
        pass


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
        "SIGMAI_API_KEY": "sigmai_api_key",
        "QQBOT_SIGMAI_API_KEY": "sigmai_api_key",
        "SIGMAI_BASE_URL": "sigmai_base_url",
        "QQBOT_SIGMAI_BASE_URL": "sigmai_base_url",
        "SIGMAI_MODEL": "sigmai_model",
        "QQBOT_SIGMAI_MODEL": "sigmai_model",
        "AGNES_API_KEY": "agnes_api_key",
        "QQBOT_AGNES_API_KEY": "agnes_api_key",
        "UAPI_API_KEY": "uapi_api_key",
        "QQBOT_UAPI_API_KEY": "uapi_api_key",
        "BILI_SESSDATA": "bili_sessdata",
        "QQBOT_BILI_SESSDATA": "bili_sessdata",
        "TOUCHGAL_API_TOKEN": "touchgal_api_token",
        "QQBOT_TOUCHGAL_API_TOKEN": "touchgal_api_token",
        "TOUCHGAL_API_BASE_URL": "touchgal_api_base_url",
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


def load_config(config_path):
    backup_path = config_path + ".last-good"
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("config root must be a JSON object")
    except (json.JSONDecodeError, OSError, ValueError) as error:
        try:
            with open(backup_path, encoding="utf-8") as handle:
                config = json.load(handle)
            if not isinstance(config, dict):
                raise ValueError("backup config root must be a JSON object")
        except (json.JSONDecodeError, OSError, ValueError):
            raise RuntimeError(
                "config.json is invalid and no valid last-good backup exists: {}".format(error)
            ) from error
        atomic_write_json(config_path, config, indent=2)
        log.error("Recovered invalid config.json from %s: %s", backup_path, error)
    atomic_write_json(backup_path, config, indent=2)
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
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
        "ai_timeout_seconds": 15,
        "sigmai_timeout_seconds": 15,
        "deepseek_timeout_seconds": 20,
        "sigmai_fallback_delay_seconds": 6,
        "connect_timeout_seconds": 5,
        "reconnect_max_delay_seconds": 60,
        "ai_concurrency": 1,
        "search_concurrency": 1,
        "vision_concurrency": 1,
        "non_explicit_judge_cooldown": 180,
        "enable_long_memory_compress": False,
        "enable_scheduler": False,
        "scheduler_timezone": "Asia/Shanghai",
        "rss_restart_mb": 700,
        "rss_log_mb": 400,
    }
    runtime = config.setdefault("runtime", {})
    for key, value in runtime_defaults.items():
        if key not in runtime:
            runtime[key] = value
            migrated = True

    private_chat = config.setdefault("private_chat", {})
    for key, value in {"enabled": False, "allowed_users": []}.items():
        if key not in private_chat:
            private_chat[key] = value
            migrated = True

    sticker_mode = config.setdefault("sticker_mode", {})
    for key, value in {"vision_analyze": False, "smart_pick": False}.items():
        if key not in sticker_mode:
            sticker_mode[key] = value
            migrated = True

        # Legacy migration: remove old chat-judge settings and Agnes chat config.
    if "natural_chat" in config:
        del config["natural_chat"]
        migrated = True
    if "agnes_base_url" in config:
        del config["agnes_base_url"]
        migrated = True
    if "agnes_model" in config:
        del config["agnes_model"]
        migrated = True
    config.setdefault("agnes_api_key", "")
    if "sigmai_base_url" not in config:
        config["sigmai_base_url"] = "https://www.sigmai.net/v1"
        migrated = True
    if "sigmai_model" not in config:
        config["sigmai_model"] = "DeepSeek-V4-Flash"
        migrated = True

    runtime = config.setdefault("runtime", {})
    if "agnes_timeout_seconds" in runtime and "sigmai_timeout_seconds" not in runtime:
        runtime["sigmai_timeout_seconds"] = runtime.pop("agnes_timeout_seconds")
        migrated = True
    if "agnes_fallback_delay_seconds" in runtime and "sigmai_fallback_delay_seconds" not in runtime:
        runtime["sigmai_fallback_delay_seconds"] = runtime.pop("agnes_fallback_delay_seconds")
        migrated = True

    uapi_defaults = {
        "daily_limit": 100,
        "reserve": 30,
        "month_limit": 3400,
    }
    uapi = config.setdefault("uapi", {})
    for key, value in uapi_defaults.items():
        if key not in uapi:
            uapi[key] = value
            migrated = True

    acg_defaults = {
        "enabled": True,
        "times": [0, 6, 12, 18],
        "count": 50,
        "batch_size": 10,
    }
    acg = config.setdefault("acg_images", {})
    for obsolete_key in ("min", "max"):
        if obsolete_key in acg:
            del acg[obsolete_key]
            migrated = True
    for key, value in acg_defaults.items():
        if key not in acg:
            acg[key] = value
            migrated = True

    hotboard_defaults = {
        "enabled": True,
        "times": [9, 21],
        "types": ["bilibili"],
    }
    hotboard = config.setdefault("hotboard_push", {})
    for key, value in hotboard_defaults.items():
        if key not in hotboard:
            hotboard[key] = value
            migrated = True

    bilibili_defaults = {
        "parse_enabled": True,
        "download_max_mb": 80,
        "poll_interval": 60,
        "uapi_fallback": True,
        "risk_cooldown_seconds": 1800,
        "official_retries": 2,
    }
    bilibili = config.setdefault("bilibili", {})
    for key, value in bilibili_defaults.items():
        if key not in bilibili:
            bilibili[key] = value
            migrated = True

    touchgal_defaults = {
        "enabled": True,
        "auto_reply": True,
        "allow_nsfw": False,
        "timeout_seconds": 8,
        "cache_ttl_seconds": 600,
        "auto_min_score": 84,
        "auto_cooldown_seconds": 20,
        "max_results": 5,
        "max_resources": 3,
        "site_base_url": "https://www.touchgal.ink",
    }
    touchgal = config.setdefault("touchgal", {})
    for key, value in touchgal_defaults.items():
        if key not in touchgal:
            touchgal[key] = value
            migrated = True

    # Merge new per-group feature defaults into existing group_defaults
    feature_defaults = {
        "bili_parse": True,
        "acg_images": True,
        "hotboard_push": True,
        "galgame_resource": True,
    }
    feats = config.setdefault("group_defaults", {}).setdefault("features", {})
    for key, value in feature_defaults.items():
        if key not in feats:
            feats[key] = value
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
