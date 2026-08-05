"""Policy-enforced gateway over registered and Agent-native tools."""

import inspect

from ..policy import tool_allowed
from .napcat import SAFE_ACTIONS, action_description, napcat_read
from .native import NATIVE_TOOL_DESCRIPTIONS, WRITE_TOOLS, execute_native


GROUP_SCOPED_REGISTRY_TOOLS = {
    "get_member_info", "get_recent_messages", "get_group_files", "get_file_url",
    "get_group_honor", "get_shut_list", "get_essence_list", "get_at_all_remain",
}
OWNER_PRIVATE_ONLY_REGISTRY_TOOLS = {"get_friend_info"}


class AgentToolGateway:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self._registry = None

    def _load(self):
        if self._registry is None:
            import ai_tools
            self._registry = {
                name: value for name, value in vars(ai_tools).items()
                if name.startswith("uapi_") or name in {
                    "get_group_info", "get_member_info", "get_recent_messages",
                    "get_group_files", "get_file_url", "get_group_notice",
                    "get_group_honor", "get_shut_list", "get_friend_info",
                    "get_image_ocr", "get_essence_list", "get_group_info_ex",
                    "check_url_safety", "translate_text", "get_at_all_remain",
                }
            }
        return self._registry

    def catalog(self):
        catalog = {name: "\u5df2\u6ce8\u518c\u7684\u53ea\u8bfb\u67e5\u8be2\u5de5\u5177" for name in self._load()}
        catalog.update({name: action_description(name) for name in SAFE_ACTIONS})
        catalog.update(NATIVE_TOOL_DESCRIPTIONS)
        return catalog

    def is_read_only(self, tool_name):
        if tool_name in NATIVE_TOOL_DESCRIPTIONS:
            return tool_name not in WRITE_TOOLS
        return tool_name in SAFE_ACTIONS or tool_name in self._load()

    async def execute(self, agent_event, tool_name, **arguments):
        if not tool_allowed(self.dispatcher.config, agent_event, tool_name):
            return {"ok": False, "error": "tool_denied_by_agent_policy", "tool": tool_name}
        if tool_name in NATIVE_TOOL_DESCRIPTIONS:
            return await execute_native(
                self.dispatcher.agent_runtime, agent_event, tool_name, arguments)
        if tool_name in SAFE_ACTIONS:
            return await napcat_read(self.dispatcher, agent_event, tool_name, **arguments)
        tool = self._load().get(tool_name)
        if tool is None or not callable(tool):
            return {"ok": False, "error": "unknown_agent_tool", "tool": tool_name}
        arguments = dict(arguments)
        if tool_name in OWNER_PRIVATE_ONLY_REGISTRY_TOOLS:
            if not agent_event.scope.is_private or not agent_event.identity.is_super_owner:
                return {"ok": False, "error": "owner_private_tool_only", "tool": tool_name}
        if tool_name in GROUP_SCOPED_REGISTRY_TOOLS:
            if agent_event.scope.is_private:
                if not agent_event.identity.is_super_owner or "group_id" not in arguments:
                    return {"ok": False, "error": "group_id_required_for_owner_private", "tool": tool_name}
            else:
                arguments["group_id"] = int(agent_event.scope.group_id)
        try:
            result = tool(self.dispatcher, **arguments)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, dict) else {"ok": True, "data": result}
        except Exception as error:
            return {"ok": False, "error": "tool_execution_failed", "message": str(error)[:300]}
