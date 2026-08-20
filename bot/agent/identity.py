"""Identity and scope resolution kept separate from response generation."""

from ..permission import get_member_role
from .models import AgentIdentity, AgentScope, IdentityLevel

def resolve_scope(config, event):
    group_id = event.get("group_id")
    user_id = int(event.get("user_id") or event.get("sender", {}).get("user_id") or 0)
    if event.get("message_type") == "group" and group_id:
        return AgentScope(kind="group", key=f"group:{int(group_id)}", group_id=int(group_id))
    return AgentScope(kind="private", key=f"owner:{user_id}", owner_id=user_id)

async def resolve_identity(dispatcher, event):
    config = dispatcher.config
    user_id = int(event.get("user_id") or event.get("sender", {}).get("user_id") or 0)
    bot_id = int(config.get("bot_qq") or 0)
    owner_id = int(config.get("bot_owner") or 0)
    if user_id == owner_id or user_id == bot_id:
        return AgentIdentity(user_id, IdentityLevel.SUPER_OWNER, "super", user_id == bot_id)
    group_id = event.get("group_id")
    if event.get("message_type") != "group" or not group_id:
        return AgentIdentity(user_id, IdentityLevel.MEMBER, "member", False)
    # NapCat's sender.role may be stale or forged; verify the group role
    # through the same cached API path as bot/permission.py instead.
    role = await get_member_role(dispatcher, int(group_id), user_id)
    if role == "owner":
        level = IdentityLevel.GROUP_OWNER
    elif role == "admin":
        level = IdentityLevel.GROUP_ADMIN
    else:
        level = IdentityLevel.MEMBER
    return AgentIdentity(user_id, level, role, False)
