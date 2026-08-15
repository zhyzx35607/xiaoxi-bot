"""Deterministic guardrails for Agent autonomy and proactive messages."""

from datetime import datetime
from ..utils import bot_timezone, now_in_timezone
from .models import AgentDecision, AgentEvent, IdentityLevel

DEFAULTS = {"enabled": True, "group_enabled": True, "private_enabled": True, "proactive_enabled": True, "owner_daily_limit": 2, "owner_hourly_limit": 1, "group_daily_limit": 3, "topic_cooldown_seconds": 1800, "quiet_start": 23, "quiet_end": 9, "member_passive_only": True}

def agent_config(config):
    result = dict(DEFAULTS)
    result.update(config.get("agent", {}) or {})
    return result

def is_quiet_hours(settings, now=None):
    hour = (now or datetime.now(bot_timezone())).hour
    start, end = int(settings["quiet_start"]), int(settings["quiet_end"])
    return (hour >= start or hour < end) if start > end else start <= hour < end

def decide_event(config, event: AgentEvent, *, explicit=False):
    settings = agent_config(config)
    if not settings["enabled"]: return AgentDecision(False, "agent_disabled")
    if event.scope.is_private and not settings["private_enabled"]: return AgentDecision(False, "private_agent_disabled")
    if not event.scope.is_private and not settings["group_enabled"]: return AgentDecision(False, "group_agent_disabled")
    if explicit: return AgentDecision(True, "explicit_request")
    if event.identity.level < IdentityLevel.GROUP_OWNER and settings["member_passive_only"]: return AgentDecision(False, "member_passive_only")
    if not settings["proactive_enabled"]: return AgentDecision(False, "proactive_disabled")
    if is_quiet_hours(settings, now_in_timezone(config)) and not event.identity.is_super_owner: return AgentDecision(False, "quiet_hours")
    return AgentDecision(True, "privileged_proactive_candidate")

def primary_router_enabled(config, event: AgentEvent):
    settings = agent_config(config)
    if not settings["enabled"]:
        return False
    if event.scope.is_private:
        return bool(
            settings.get("primary_router", False)
            and not settings.get("observation_only", True)
            and settings.get("owner_autonomy_enabled", False)
            and event.identity.is_super_owner)
    group = config.get("groups", {}).get(str(event.scope.group_id), {})
    group_agent = group.get("agent", {}) if isinstance(group, dict) else {}
    return bool(group_agent.get("primary_router", False))

def tool_allowed(config, event: AgentEvent, tool_name):
    # Moderation actions (set_group_ban/set_group_kick/set_group_whole_ban) are
    # not gated here: they can never reach this point because the real boundary
    # is the gateway allowlist — SAFE_ACTIONS only contains risk=="read" NapCat
    # actions, and native write tools check can_manage_agent in execute_native.
    forbidden = {"get_clientkey", "get_cookies", "get_credentials", "get_csrf_token", "get_rkey", "get_rkey_server", "nc_get_rkey", "send_packet", "send_raw_packet", "test_action"}
    if tool_name in forbidden: return False
    return True
