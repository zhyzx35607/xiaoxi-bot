"""Response policy for staged Agent rollout."""

from .models import IdentityLevel
from .policy import primary_router_enabled


def can_autosend(config, agent_event, plan, *, explicit=False):
    if plan.get("needs_confirmation", True) and not agent_event.identity.is_super_owner:
        return False, "confirmation_required"
    if agent_event.scope.is_private and config.get("agent", {}).get("observation_only", True):
        return False, "observation_only"
    if not primary_router_enabled(config, agent_event):
        return False, "router_not_authorized"
    if agent_event.scope.is_private and agent_event.identity.is_super_owner:
        return True, "super_owner_private"
    if not agent_event.scope.is_private and agent_event.identity.level >= IdentityLevel.GROUP_OWNER:
        return True, "privileged_group_scope"
    # 显式呼叫（@bot/叫名字/回复 bot）时普通成员也可获得 Agent 回复；
    # 写能力仍由工具层按身份门控，规划只读/聊天不受限。
    if not agent_event.scope.is_private and explicit:
        return True, "explicit_group_request"
    return False, "scope_not_authorized"
