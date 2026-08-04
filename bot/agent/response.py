"""Response policy for staged Agent rollout."""

from .models import IdentityLevel


def can_autosend(config, agent_event, plan):
    if config.get("agent", {}).get("observation_only", True):
        return False, "observation_only"
    if plan.get("needs_confirmation", True) and not agent_event.identity.is_super_owner:
        return False, "confirmation_required"
    if agent_event.scope.is_private and agent_event.identity.is_super_owner:
        return True, "super_owner_private"
    if not agent_event.scope.is_private and agent_event.identity.level >= IdentityLevel.GROUP_OWNER:
        return True, "privileged_group_scope"
    return False, "scope_not_authorized"
