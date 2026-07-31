"""Dispatcher registration and top-level event routing."""

import logging

from ..permission import check_permission
from .context import _event_scope_allowed

log = logging.getLogger("qqbot")

class RouterMixin:
    def register(self, name, handler, help_text="", admin_only=False, owner_only=False,
                 bot_owner=False, bot_admin_required=False, bot_owner_required=False,
                 bot_owner_only=False):
        self.commands[name] = {
            "handler": handler, "help": help_text,
            "admin_only": admin_only, "owner_only": owner_only, "bot_owner": bot_owner,
            "bot_admin_required": bot_admin_required, "bot_owner_required": bot_owner_required,
            "bot_owner_only": bot_owner_only,
        }

    async def dispatch(self, event):
        try:
            if not _event_scope_allowed(self, event):
                return
            pt = event.get("post_type", "")
            if pt == "message":
                await self._handle_message(event)
            elif pt == "message_sent":
                # Bot's own outgoing messages — feed into context buffer
                # so AI knows what it just said (self-awareness)
                await self._handle_self_message(event)
            elif pt == "notice":
                from ..notice_handler import handle_notice
                await handle_notice(self, event)
            elif pt == "request":
                from ..request_handler import handle_request
                await handle_request(self, event)
        except Exception as e:
            log.error("Dispatch error: %s", e, exc_info=True)

    async def _run_command(self, cmd, args, group_id, user_id, role, sender_card, message):
        cmd_info = self.commands.get(cmd)
        if not cmd_info:
            return

        # Permission check
        allowed, error = await check_permission(self, group_id, user_id, role, cmd_info)
        if not allowed:
            if error:
                await self._reply(group_id, user_id, error)
            return

        try:
            await cmd_info["handler"](self, group_id, user_id, args, role, sender_card, message)
        except Exception as e:
            log.error("Command %s error: %s", cmd, e, exc_info=True)
            await self._reply(group_id, user_id, "出错了，等会再试。")
