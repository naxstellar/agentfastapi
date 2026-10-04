"""MCP tool registry (WP6B): tools from the servers in `MCP_SERVERS`, merged into the agent's tool list.

A server that is down or misconfigured is skipped (logged by type only), so it can never break a run. The
loaded tools are cached briefly: listing them connects to every server. A failing tool call comes back as an
error tool result (the agent's ToolNode converts exceptions), not as a crashed run.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import MCP_SERVERS

logger = logging.getLogger("app.agent.mcp")

LOAD_TIMEOUT_S = 15
CACHE_TTL_S = 300
_cache: dict[str, tuple[float, list[BaseTool]]] = {}


async def _load_server(name: str, config: dict) -> list[BaseTool]:
    client = MultiServerMCPClient({name: config})
    async with asyncio.timeout(LOAD_TIMEOUT_S):
        return await client.get_tools()


async def load_mcp_tools(servers: dict | None = None, *, reserved: frozenset[str] = frozenset()) -> list[BaseTool]:
    """Tools of every reachable server. Never raises."""
    servers = MCP_SERVERS if servers is None else servers
    tools: list[BaseTool] = []
    taken = set(reserved)
    for name, config in servers.items():
        cached = _cache.get(name)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
            server_tools = cached[1]
        else:
            try:
                server_tools = await _load_server(name, config)
            except Exception as exc:  # BaseExceptionGroup from the transport is an Exception subclass too
                logger.warning("MCP server %r unavailable: %s", name, type(exc).__name__)
                continue
            _cache[name] = (time.monotonic(), server_tools)
        for tool in server_tools:
            if tool.name in taken:
                logger.warning("MCP server %r: tool %r skipped (name already in use)", name, tool.name)
                continue
            taken.add(tool.name)
            tools.append(tool)
    return tools


class McpToolErrors(AgentMiddleware):
    """Turns any failure of an MCP tool call into an error tool result. The adapter only converts errors the server
    reports; a dropped connection or a timeout would otherwise raise out of the tool node and fail the run."""

    def __init__(self, tools: list[BaseTool]):
        self._names = {t.name for t in tools}

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        call = request.tool_call
        if call["name"] not in self._names:
            return await handler(request)
        try:
            return await handler(request)
        except Exception as exc:  # CancelledError is a BaseException and passes through
            logger.warning("MCP tool %r failed: %s", call["name"], type(exc).__name__)
            return ToolMessage(
                f"Error: the tool {call['name']!r} failed ({type(exc).__name__}). Try again or use another approach.",
                name=call["name"],
                tool_call_id=call["id"],
                status="error",
            )


def clear_cache() -> None:
    _cache.clear()
