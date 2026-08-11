"""Configuration loading, recovery, migration, and environment overrides."""

import json
import logging
import os

from bot.utils import atomic_write_json

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
        "MUKYU_API_KEY": "mukyu_api_key",
        "QQBOT_MUKYU_API_KEY": "mukyu_api_key",
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

def _read_config_object(path, label):
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"{label} root must be a JSON object")
    return config


def load_config(config_path):
    backup_path = config_path + ".last-good"
    try:
        config = _read_config_object(config_path, "config")
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        try:
            config = _read_config_object(backup_path, "backup config")
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise RuntimeError(
                f"config.json is invalid and no valid last-good backup exists: {error}"
            ) from error
        except OSError as backup_error:
            raise RuntimeError(
                f"cannot read config.json last-good backup: {backup_error}"
            ) from backup_error
        try:
            atomic_write_json(config_path, config, indent=2)
        except OSError as restore_error:
            raise RuntimeError(
                f"cannot restore config.json from last-good backup: {restore_error}"
            ) from restore_error
        log.error("Recovered invalid config.json from %s: %s", backup_path, error)
    except OSError as error:
        raise RuntimeError(f"cannot read config.json: {error}") from error
    try:
        atomic_write_json(backup_path, config, indent=2)
    except OSError as backup_error:
        raise RuntimeError(f"cannot update config.json last-good backup: {backup_error}") from backup_error
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
        "startup_connect_timeout_seconds": 30,
        "reconnect_max_delay_seconds": 60,
        "ai_concurrency": 1,
        "search_concurrency": 1,
        "vision_concurrency": 1,
        "media_timeout_seconds": 12,
        "image_ocr_max_attempts": 2,
        "owner_private_merge_seconds": 5,
        "owner_reply_similarity_cooldown_seconds": 300,
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
        "concurrency": 3,
        "daily_limit": 100,
        "reserve": 30,
        "month_limit": 3400,
    }
    uapi = config.setdefault("uapi", {})
    for key, value in uapi_defaults.items():
        if key not in uapi:
            uapi[key] = value
            migrated = True

    mukyu_defaults = {
        "enabled": True,
        "base_url": "https://i.mukyu.ru",
        "timeout_seconds": 20,
        "max_json_bytes": 262144,
        "command_cooldown_seconds": 10,
    }
    mukyu = config.setdefault("mukyu_images", {})
    for key, value in mukyu_defaults.items():
        if key not in mukyu:
            mukyu[key] = value
            migrated = True

    voice_reply_defaults = {
        "enabled": False,
        "probability": 0.08,
        "min_chars": 5,
        "max_chars": 45,
        "cooldown_seconds": 3600,
        "daily_limit": 2,
        "character_id": "lucy-voice-xueling",
    }
    voice_reply = config.setdefault("voice_reply", {})
    for key, value in voice_reply_defaults.items():
        if key not in voice_reply:
            voice_reply[key] = value
            migrated = True

    acg_defaults = {
        "enabled": False,
        "provider": "mukyu",
        "send_count": 20,
        "minimum_count": 20,
        "dedupe_days": 7,
        "collector_interval_seconds": 5,
        "tags": [],
        "tag_mode": "or",
        "orientation": "landscape",
        "min_pixels": 1000000,
        "min_bookmarks": 0,
        "ai_type": None,
        "illust_type": None,
        "max_delivery_attempts": 3,
        "retry_base_seconds": 300,
        "retry_max_seconds": 1800,
        "delivery_ttl_seconds": 7200,
        "windows": [
            ["08:00", "11:00"], ["12:00", "15:00"],
            ["16:00", "19:00"], ["20:00", "23:00"],
        ],
    }
    acg = config.setdefault("acg_images", {})
    for obsolete_key in ("min", "max", "times", "count", "batch_size"):
        if obsolete_key in acg:
            del acg[obsolete_key]
            migrated = True
    for key, value in acg_defaults.items():
        if key not in acg:
            acg[key] = value
            migrated = True
    if str(acg.get("provider") or "").strip().lower() != "mukyu":
        acg["provider"] = "mukyu"
        migrated = True
    interval = max(5, int(acg.get("collector_interval_seconds", 5) or 5))
    if acg.get("collector_interval_seconds") != interval:
        acg["collector_interval_seconds"] = interval
        migrated = True

    hotboard_defaults = {
        "enabled": False,
        "types": ["weibo"],
        "detail_count": 10,
        "windows": [["10:00", "13:00"], ["19:00", "22:00"]],
    }
    hotboard = config.setdefault("hotboard_push", {})
    if "times" in hotboard:
        del hotboard["times"]
        migrated = True
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
        "acg_images": False,
        "hotboard_push": False,
        "galgame_resource": True,
        "voice_reply": False,
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

    message_output_defaults = {
        "forward_threshold_chars": 200,
        "forward_node_target_chars": 800,
        "help_always_forward": True,
        "ai_summary_enabled": True,
        "ai_summary_max_chars": 80,
        "ai_summary_timeout_seconds": 4,
        "reply_to_forward": True,
    }
    message_output = config.setdefault("message_output", {})
    for key, value in message_output_defaults.items():
        if key not in message_output:
            message_output[key] = value
            migrated = True

    persona_defaults = {
        "role_awareness_enabled": True,
        "super_owner_name": "主人",
        "master_name": "主人",
        "gowner_name": "群主",
        "admin_name": "管理",
    }
    persona = config.setdefault("persona", {})
    for key, value in persona_defaults.items():
        if key not in persona:
            persona[key] = value
            migrated = True

    category_defaults = {
        "message": False,
        "management": False,
        "todo": False,
        "album": False,
        "file": False,
        "friend": False,
        "account": False,
        "interaction": False,
        "experimental": False,
    }
    napcat_features = config.setdefault("napcat_features", {})
    ai_tools = config.setdefault("ai_tools", {})
    for key, value in category_defaults.items():
        if key not in napcat_features:
            napcat_features[key] = value
            migrated = True
        if key not in ai_tools:
            ai_tools[key] = value
            migrated = True

    automation_defaults = {
        "enabled": False,
        "allow_light_management": False,
        "max_rules": 20,
        "max_daily_actions": 30,
    }
    automation = config.setdefault("automation", {})
    for key, value in automation_defaults.items():
        if key not in automation:
            automation[key] = value
            migrated = True
    roleplay_defaults = {
        "enabled": True,
        "database_path": "data/roleplay.sqlite3",
        "import_directory": "data/roleplay_imports",
        "private_policy_path": "data/roleplay_private/policies.json",
        "session_timeout_seconds": 1800,
        "recent_message_limit": 20,
        "memory_recall_limit": 10,
        "summary_every_messages": 20,
        "max_context_chars": 18000,
        "max_history_chars": 12000,
        "max_history_message_chars": 4000,
        "response_max_tokens": 1200,
        "story_response_max_tokens": 2000,
        "response_temperature": 0.82,
        "message_chunk_chars": 900,
        "max_message_segments": 10,
        "max_messages_per_chat": 5000,
        "max_story_beats_per_chat": 1000,
        "max_summaries_per_chat": 50,
        "audit_retention_days": 90,
        "retention_cleanup_every_messages": 100,
        "lightrag": {
            "enabled": False,
            "base_url": "http://127.0.0.1:8020",
            "mode": "hybrid",
            "timeout_seconds": 4,
            "max_context_chars": 5000,
        },
    }
    roleplay = config.setdefault("roleplay", {})
    # Older roleplay releases allowed story requests without a provider-side
    # token cap. Remove that unsafe switch when migrating existing installs.
    if "story_unbounded_tokens" in roleplay:
        del roleplay["story_unbounded_tokens"]
        migrated = True
    for key, value in roleplay_defaults.items():
        if key not in roleplay:
            roleplay[key] = value
            migrated = True
    if not isinstance(roleplay.get("lightrag"), dict):
        roleplay["lightrag"] = dict(roleplay_defaults["lightrag"])
        migrated = True
    else:
        for key, value in roleplay_defaults["lightrag"].items():
            if key not in roleplay["lightrag"]:
                roleplay["lightrag"][key] = value
                migrated = True
    agent_defaults = {
        "enabled": True,
        "group_enabled": True,
        "private_enabled": True,
        "proactive_enabled": True,
        "owner_daily_limit": 2,
        "owner_hourly_limit": 1,
        "group_daily_limit": 3,
        "topic_cooldown_seconds": 1800,
        "rejection_mute_seconds": 43200,
        "quiet_start": 23,
        "quiet_end": 9,
        "member_passive_only": True,
        "observation_enabled": False,
        "event_history_limit": 100,
        "observation_only": True,
        "primary_router": False,
        "owner_autonomy_enabled": False,
        "owner_max_rounds": 6,
        "owner_tool_budget": 12,
        "group_max_rounds": 3,
        "group_tool_budget": 5,
        "planner_max_tokens": 1400,
        "tool_timeout_seconds": 15,
        "background_tasks_enabled": True,
        "background_task_max_attempts": 3,
        "background_task_lease_seconds": 3600,
        "owner_goal_check_interval_seconds": 7200,
        "group_review_interval_seconds": 10800,
        "review_lease_seconds": 3600,
        "worker_enabled": True,
        "worker_interval_seconds": 30,
        "companion_enabled": True,
        "companion_min_gap_seconds": 21600,
        "companion_idle_seconds": 28800,
        "companion_max_tokens": 700,
        "companion_temperature": 0.85,
        "companion_outbox_max_attempts": 3,
        "owner_group_direct_reply": True,
    }
    agent = config.setdefault("agent", {})
    for key, value in agent_defaults.items():
        if key not in agent:
            agent[key] = value
            migrated = True
    if int(agent.get("schema_version", 0) or 0) < 2:
        agent["schema_version"] = 2
        agent["worker_enabled"] = True
        agent["worker_interval_seconds"] = 30
        migrated = True
    if int(agent.get("schema_version", 0) or 0) < 3:
        agent["schema_version"] = 3
        agent.setdefault("group_max_rounds", 3)
        agent.setdefault("group_tool_budget", 5)
        agent.setdefault("planner_max_tokens", 1400)
        agent.setdefault("group_review_interval_seconds", 10800)
        agent.setdefault("rejection_mute_seconds", 43200)
        migrated = True
    if int(agent.get("schema_version", 0) or 0) < 4:
        agent["schema_version"] = 4
        if int(agent.get("owner_daily_limit", 6) or 6) == 6:
            agent["owner_daily_limit"] = 12
        agent.setdefault("owner_hourly_limit", 3)
        agent.setdefault("owner_hourly_limit", 3)
        agent.setdefault("companion_enabled", True)
        agent.setdefault("companion_min_gap_seconds", 1800)
        agent.setdefault("companion_max_tokens", 700)
        agent.setdefault("companion_temperature", 0.85)
        agent.setdefault("companion_outbox_max_attempts", 3)
        agent.setdefault("owner_group_direct_reply", True)
        migrated = True
    if int(agent.get("schema_version", 0) or 0) < 5:
        agent["schema_version"] = 5
        agent.setdefault("owner_daily_limit", 2)
        agent.setdefault("owner_hourly_limit", 1)
        agent.setdefault("companion_min_gap_seconds", 21600)
        agent.setdefault("companion_idle_seconds", 28800)
        migrated = True
    return config, migrated
