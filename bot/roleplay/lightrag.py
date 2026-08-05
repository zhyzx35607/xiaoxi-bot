"""Small, failure-tolerant LightRAG HTTP client."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger("qqbot")


class LightRAGClient:
    def __init__(self, config: dict[str, Any], session: aiohttp.ClientSession | None = None):
        self.config = config
        self.session = session

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False) and self.config.get("base_url"))

    async def health(self) -> bool:
        if not self.enabled:
            return False
        try:
            result = await self._request("GET", "/health")
            return isinstance(result, dict)
        except Exception:
            return False

    async def query(self, query: str, *, mode: str | None = None) -> str:
        if not self.enabled or not query.strip():
            return ""
        payload = {
            "query": query[:4000],
            "mode": mode or self.config.get("mode", "hybrid"),
            "only_need_context": True,
        }
        try:
            result = await self._request("POST", "/query", json=payload)
        except Exception as exc:
            log.warning("LightRAG query degraded: %s", exc)
            return ""
        if isinstance(result, str):
            return result[: int(self.config.get("max_context_chars", 5000))]
        if isinstance(result, dict):
            for key in ("response", "result", "context", "data", "message"):
                value = result.get(key)
                if isinstance(value, str):
                    return value[: int(self.config.get("max_context_chars", 5000))]
        return ""

    async def insert(self, text: str) -> bool:
        if not self.enabled or not text.strip():
            return False
        try:
            await self._request("POST", "/insert", json={"text": text[:100000]})
            return True
        except Exception as exc:
            log.warning("LightRAG insert failed: %s", exc)
            return False

    async def _request(self, method: str, path: str, **kwargs):
        base = str(self.config.get("base_url", "")).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=float(self.config.get("timeout_seconds", 4)))
        headers = {}
        token = str(self.config.get("api_key") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        own_session = self.session is None or self.session.closed
        session = aiohttp.ClientSession(timeout=timeout) if own_session else self.session
        try:
            async with session.request(method, base + path, headers=headers, timeout=timeout, **kwargs) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    return await response.json()
                return await response.text()
        finally:
            if own_session:
                await session.close()
