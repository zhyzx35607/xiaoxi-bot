"""bot/permission.py - Unified permission system for QQ Bot"""
import copy
import json
import threading
import time
import logging
from .utils import atomic_write_json
log = logging.getLogger("qqbot")
# Serializes config.json writes between the command path (commands/common._commit)
# and the in-memory path (save_group_config). A threading lock is required because
# save_group_config also runs inside a worker thread via asyncio.to_thread.
_CONFIG_WRITE_LOCK = threading.Lock()
LEVEL_SUPER = 5
LEVEL_MASTER = 4
LEVEL_GOWNER = 3
LEVEL_ADMIN = 2
LEVEL_MEMBER = 1
LEVEL_NAMES = {5: "super", 4: "master", 3: "gowner", 2: "admin", 1: "member"}
def get_group_config(dispatcher, group_id):
    if not group_id:
        return {}
    gid = str(group_id)
    defaults = dispatcher.config.get("group_defaults", {})
    groups = dispatcher.config.get("groups", {})
    group_cfg = groups.get(gid, {})
    merged = {
        "enabled": group_cfg.get("enabled", True),
        "masters": group_cfg.get("masters", []),
        "welcome_msg": {**defaults.get("welcome_msg", {}), **group_cfg.get("welcome_msg", {})},
        "bad_words": {**defaults.get("bad_words", {}), **group_cfg.get("bad_words", {})},
        "features": {**defaults.get("features", {}), **group_cfg.get("features", {})},
        "napcat_features": {
            **dispatcher.config.get("napcat_features", {}),
            **group_cfg.get("napcat_features", {}),
        },
        "ai_tools": {
            **dispatcher.config.get("ai_tools", {}),
            **group_cfg.get("ai_tools", {}),
        },
        "automation": {
            **dispatcher.config.get("automation", {}),
            **group_cfg.get("automation", {}),
        },
    }
    # Merge bad_words as union of default and group-specific lists
    default_words = set(defaults.get("bad_words", {}).get("words", []))
    group_words = set(group_cfg.get("bad_words", {}).get("words", []))
    merged["bad_words"]["words"] = sorted(list(default_words | group_words))
    return merged
def is_group_enabled(dispatcher, group_id):
    if not group_id:
        return False
    gid = str(group_id)
    groups = dispatcher.config.get("groups", {})
    return groups.get(gid, {}).get("enabled", False)
async def _resolve_user_level(dispatcher, group_id, user_id, sender_role_hint=""):
    """Resolve a caller role and report whether the result is authoritative."""
    # Bot owner has super level everywhere
    if user_id == dispatcher.config.get("bot_owner"):
        return LEVEL_SUPER, "super", True
    # The bot account itself also has super level (self-commands via message_sent)
    if user_id == dispatcher.config.get("bot_qq"):
        return LEVEL_SUPER, "super", True
    if not group_id:
        return LEVEL_MEMBER, "member", True
    gcfg = get_group_config(dispatcher, group_id)
    masters = gcfg.get("masters", [])
    if user_id in masters:
        return LEVEL_MASTER, "master", True
    # Query real-time role from API (NOT NapCat's possibly-stale sender.role)
    role_map = {"owner": (LEVEL_GOWNER, "gowner"), "admin": (LEVEL_ADMIN, "admin"), "member": (LEVEL_MEMBER, "member")}
    try:
        r = await dispatcher.client.get_group_member_info(group_id, user_id)
        if r.get("status") == "ok":
            data = r.get("data", {})
            real_role = data.get("role")
            log.debug("get_user_level: user=%s group=%s api_role=%s hint=%s",
                     user_id, group_id, real_role, sender_role_hint)
            if real_role in role_map:
                level, name = role_map[real_role]
                return level, name, True
            log.warning("get_user_level returned an invalid role for group=%s", group_id)
    except Exception as e:
        log.warning("get_user_level API failed for group=%s: %s", group_id, e)
    return LEVEL_MEMBER, "member", False


async def get_user_level(dispatcher, group_id, user_id, sender_role_hint=""):
    level, name, _ = await _resolve_user_level(
        dispatcher, group_id, user_id, sender_role_hint)
    return level, name
# Member role cache (per group+user, 60s TTL) for per-message hot paths such
# as Agent identity resolution. Command permission checks keep querying the
# API on every call via _resolve_user_level.
_member_role_cache = {}
_member_role_cache_ttl = 60
_MEMBER_ROLE_CACHE_MAX_AGE = 300  # hard eviction after 5 minutes
async def get_member_role(dispatcher, group_id, user_id):
    """Real-time member role with a short TTL cache; fails closed to member."""
    if not group_id:
        return 'member'
    now = time.time()
    # Periodic cleanup of stale cache entries
    stale = [k for k, v in _member_role_cache.items() if now - v.get('ts', 0) > _MEMBER_ROLE_CACHE_MAX_AGE]
    for k in stale:
        del _member_role_cache[k]
    key = (int(group_id), int(user_id))
    cached = _member_role_cache.get(key)
    if cached and (now - cached['ts']) < _member_role_cache_ttl:
        return cached['role']
    try:
        r = await dispatcher.client.get_group_member_info(group_id, user_id)
        if r.get('status') == 'ok':
            role = r.get('data', {}).get('role', 'member')
            if role in ('owner', 'admin', 'member'):
                _member_role_cache[key] = {'role': role, 'ts': now}
                return role
            log.warning('get_member_role returned an invalid role for group=%s', group_id)
    except Exception as e:
        log.warning('get_member_role API failed for group=%s user=%s: %s', group_id, user_id, e)
    return 'member'
# Role cache (per-group, 60s TTL)
_bot_role_cache = {}
_bot_role_cache_ttl = 60
_BOT_ROLE_CACHE_MAX_AGE = 300  # hard eviction after 5 minutes
async def get_bot_role(dispatcher, group_id):
    if not group_id:
        log.warning('get_bot_role: no group_id')
        return 'member', 'member'
    now = time.time()
    # Periodic cleanup of stale cache entries
    stale = [g for g, v in _bot_role_cache.items() if now - v.get('ts', 0) > _BOT_ROLE_CACHE_MAX_AGE]
    for g in stale:
        del _bot_role_cache[g]
    cached = _bot_role_cache.get(group_id)
    if cached and (now - cached['ts']) < _bot_role_cache_ttl:
        return cached['role'], cached['role']
    bot_qq = dispatcher.config['bot_qq']
    try:
        r = await dispatcher.client.get_group_member_info(group_id, bot_qq)
        if r.get('status') == 'ok':
            role = r.get('data', {}).get('role', 'member')
            _bot_role_cache[group_id] = {'role': role, 'ts': now}
            return role, role
    except Exception as e:
        log.error('get_bot_role failed g=%s: %s', group_id, e)
    return 'member', 'member'
async def check_permission(dispatcher, group_id, user_id, sender_role, cmd_info):
    """Unified permission check.
    
    Hierarchy:
      - Bot Owner + bot account (level 5 super): bypass ALL checks
      - bot_owner_only commands (/master): bot_owner or bot_qq only
      - bot_owner commands (/enable /disable /list /clearai): bot_owner, bot_qq, or group masters
      - admin_only commands: must be group admin/owner OR master
      - bot_admin_required: bot must be admin/owner in this group
    """
    owner = dispatcher.config.get("bot_owner")
    bot_qq = dispatcher.config.get("bot_qq")
    caller_level, caller_name, caller_verified = await _resolve_user_level(
        dispatcher, group_id, user_id, sender_role)
    privileged = any(cmd_info.get(key) for key in (
        "owner_only", "bot_owner_only", "bot_owner", "admin_only",
    ))
    if privileged and not caller_verified:
        return False, "暂时无法核验你的群身份，请稍后再试"
    # Some QQ operations are owner-only for the bot account itself, such as group special titles.
    # Caller privilege cannot bypass QQ's real group-role restriction.
    if cmd_info.get("bot_owner_required"):
        bot_role_str, _ = await get_bot_role(dispatcher, group_id)
        if bot_role_str != "owner":
            return False, "这个只有群主号能做，我现在不是群主"
    # Bot must be admin/owner in the group; checked before all caller bypasses
    # so masters/supers get a clear error instead of an API failure downstream.
    if cmd_info.get("bot_admin_required"):
        bot_role_str, _ = await get_bot_role(dispatcher, group_id)
        if bot_role_str not in ("admin", "owner"):
            return False, "我现在不是管理员，做不了这个"
    # The configured bot owner bypasses all group-level checks.
    if user_id == owner:
        return True, None
    # Group-owner commands are scoped to the current group; super owners bypass above.
    if cmd_info.get("owner_only") and caller_level < LEVEL_GOWNER:
        return False, "需要群主或最高主人权限"
    # /master command: only bot owner can use (already handled above, this is for safety)
    if cmd_info.get("bot_owner_only"):
        if user_id == bot_qq:
            return True, None
        return False, "只有最高主人能使用此命令"
    # Commands for bot owner + bot_qq + group masters
    if cmd_info.get("bot_owner"):
        if user_id == bot_qq:
            return True, None
        if caller_level < LEVEL_MASTER:
            return False, "只有群主人或机器人账号能使用此命令"
        return True, None
    # Masters bypass admin checks
    if caller_level >= LEVEL_MASTER:
        return True, None
    # Admin-only commands
    if cmd_info.get("admin_only"):
        if caller_level < LEVEL_ADMIN:
            return False, "需要管理员权限"
    return True, None
async def can_moderate_target(dispatcher, group_id, actor_id, target_id, actor_role="member"):
    """Enforce role hierarchy for kick/ban style operations."""
    owner = dispatcher.config.get("bot_owner")
    bot_qq = dispatcher.config.get("bot_qq")
    if target_id in {owner, bot_qq}:
        return False, "这个目标受保护"
    actor_level, _, actor_verified = await _resolve_user_level(
        dispatcher, group_id, actor_id, actor_role)
    if not actor_verified:
        return False, "暂时无法核验操作人的群身份"
    target_level, _, target_verified = await _resolve_user_level(
        dispatcher, group_id, target_id, "member")
    if not target_verified:
        return False, "暂时无法核验目标成员的群身份"
    # QQ never allows operating on the group owner, regardless of internal levels.
    if target_level == LEVEL_GOWNER:
        return False, "不能操作群主"
    if actor_level < LEVEL_SUPER and target_level >= actor_level:
        return False, "不能操作同级或更高权限的成员"
    return True, None
def add_master(dispatcher, group_id, master_qq):
    gid = str(group_id)
    groups = dispatcher.config.setdefault("groups", {})
    if gid not in groups:
        groups[gid] = {"enabled": False, "masters": [], "welcome_msg": {}, "bad_words": {}, "features": {}}
    gcfg = groups[gid]
    masters = gcfg.setdefault("masters", [])
    if master_qq not in masters:
        masters.append(master_qq)
        save_group_config(dispatcher)
        return True
    return False
def remove_master(dispatcher, group_id, master_qq):
    gid = str(group_id)
    groups = dispatcher.config.get("groups", {})
    if gid in groups:
        masters = groups[gid].get("masters", [])
        if master_qq in masters:
            masters.remove(master_qq)
            save_group_config(dispatcher)
            return True
    return False
def list_masters(dispatcher, group_id):
    gcfg = get_group_config(dispatcher, group_id)
    return gcfg.get("masters", [])
def save_group_config(dispatcher):
    with _CONFIG_WRITE_LOCK:
        cfg = copy.deepcopy(dispatcher.config)
        # Never persist secrets to disk
        for secret_key in ("token", "deepseek_api_key", "sigmai_api_key", "agnes_api_key", "uapi_api_key", "mukyu_api_key", "bili_sessdata", "touchgal_api_token"):
            cfg.pop(secret_key, None)
        if isinstance(cfg.get("vision_api"), dict):
            cfg["vision_api"].pop("api_key", None)
        for gcfg in cfg.get("groups", {}).values():
            if isinstance(gcfg, dict):
                for secret_key in ("token", "deepseek_api_key", "sigmai_api_key", "agnes_api_key", "uapi_api_key", "mukyu_api_key", "bili_sessdata", "touchgal_api_token"):
                    gcfg.pop(secret_key, None)
                if isinstance(gcfg.get("vision_api"), dict):
                    gcfg["vision_api"].pop("api_key", None)
        atomic_write_json(dispatcher._config_path, cfg, indent=2)
