"""bot/permission.py - Unified permission system for QQ Bot"""

import json
import time
import logging
from .utils import atomic_write_json

log = logging.getLogger("qqbot")

LEVEL_MASTER = 4
LEVEL_ADMIN = 2
LEVEL_MEMBER = 1

LEVEL_NAMES = {4: "master", 2: "admin", 1: "member"}


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
    }
    return merged


def is_group_enabled(dispatcher, group_id):
    if not group_id:
        return False
    gid = str(group_id)
    groups = dispatcher.config.get("groups", {})
    return groups.get(gid, {}).get("enabled", False)


async def get_user_level(dispatcher, group_id, user_id, sender_role_hint=""):
    # Bot owner has master level everywhere
    if user_id == dispatcher.config.get("bot_owner"):
        return LEVEL_MASTER, "master"

    if not group_id:
        return LEVEL_MEMBER, "member"
    gcfg = get_group_config(dispatcher, group_id)
    masters = gcfg.get("masters", [])
    if user_id in masters:
        return LEVEL_MASTER, "master"
    # Query real-time role from API (NOT NapCat's possibly-stale sender.role)
    role_map = {"owner": (LEVEL_ADMIN, "admin"), "admin": (LEVEL_ADMIN, "admin"), "member": (LEVEL_MEMBER, "member")}
    try:
        r = await dispatcher.client.get_group_member_info(group_id, user_id)
        if r.get("status") == "ok":
            data = r.get("data", {})
            real_role = data.get("role", sender_role_hint or "member")
            log.info("get_user_level: user=%s group=%s api_role=%s hint=%s",
                     user_id, group_id, real_role, sender_role_hint)
            return role_map.get(real_role, (LEVEL_MEMBER, "member"))
    except Exception as e:
        log.warning("get_user_level API failed for user=%s: %s, using hint=%s",
                    user_id, e, sender_role_hint)
    return role_map.get(sender_role_hint or "member", (LEVEL_MEMBER, "member"))


# Role cache (per-group, 60s TTL)
_bot_role_cache = {}
_bot_role_cache_ttl = 60


async def get_bot_role(dispatcher, group_id):
    if not group_id:
        log.warning('get_bot_role: no group_id')
        return 'member', 'member'
    now = time.time()
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
    if cached:
        return cached['role'], cached['role']
    return 'member', 'member'


async def check_permission(dispatcher, group_id, user_id, sender_role, cmd_info):
    """Unified permission check.
    
    Hierarchy:
      - Bot Owner (config.bot_owner): bypasses ALL checks
      - bot_owner_only commands (/master): ONLY bot_owner
      - bot_owner commands (/enable /disable /list /clearai): bot_owner, bot_qq, or group masters
      - admin_only commands: must be group admin/owner OR master
      - bot_admin_required: bot must be admin/owner in this group
    """
    owner = dispatcher.config.get("bot_owner")
    bot_qq = dispatcher.config.get("bot_qq")
    caller_level, caller_name = await get_user_level(dispatcher, group_id, user_id, sender_role)

    # Some QQ operations are owner-only for the bot account itself, such as group special titles.
    # Caller privilege cannot bypass QQ's real group-role restriction.
    if cmd_info.get("bot_owner_required"):
        bot_role_str, _ = await get_bot_role(dispatcher, group_id)
        if bot_role_str != "owner":
            return False, "这个只有群主号能做，我现在不是群主"

    # Bot owner (446697984) bypasses ALL checks
    if user_id == owner:
        return True, None

    # /master command: only bot owner can use (already handled above, this is for safety)
    if cmd_info.get("bot_owner_only"):
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

    # Bot must be admin/owner in the group
    if cmd_info.get("bot_admin_required"):
        bot_role_str, _ = await get_bot_role(dispatcher, group_id)
        if bot_role_str not in ("admin", "owner"):
            return False, "我现在不是管理员，做不了这个"

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
    atomic_write_json(dispatcher._config_path, dispatcher.config, indent=2)
