"""Bounded execution of structured Agent tool plans."""

import asyncio
import logging

from .policy import tool_allowed

log = logging.getLogger("qqbot")


class AgentExecutor:
    def __init__(self, gateway, config):
        self.gateway = gateway
        self.config = config

    async def execute(self, agent_event, tool_calls, *, remaining_budget):
        results = []
        timeout = max(3, min(int(self.config.get("agent", {}).get("tool_timeout_seconds", 15)), 60))
        for item in tool_calls[:remaining_budget]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")[:80]
            step_id = str(item.get("step_id") or "")[:30]
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            if not name or not tool_allowed(self.config, agent_event, name):
                results.append({"name": name, "step_id": step_id, "ok": False, "error": "tool_denied_by_agent_policy"})
                continue
            try:
                result = await asyncio.wait_for(
                    self.gateway.execute(agent_event, name, **arguments), timeout=timeout)
            except asyncio.TimeoutError:
                result = {"ok": False, "error": "tool_timeout"}
            except Exception as error:
                # A single broken tool (e.g. OneBot connection drop inside
                # gateway.execute) must not abort the whole message dispatch.
                log.warning("Agent tool %s failed: %s: %s", name,
                            type(error).__name__, error)
                result = {"ok": False, "error": "tool_execution_failed"}
            results.append({"name": name, "step_id": step_id, "arguments": arguments, "result": result})
        return results
