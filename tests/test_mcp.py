"""WP6B: MCP tools are loaded from MCP_SERVERS, callable by the agent, and a broken server or tool never breaks a run."""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent import mcp
from app.agent.graph import RESERVED_TOOL_NAMES, build_agent
from tests.fakes import ScriptedChatModel
from tests.test_agent_graph import persistence, run_turn, tool_call  # noqa: F401
from tests.test_agent_graph import manager, new_turn, provider, session_factory, storage  # noqa: F401

SAMPLE = {"transport": "stdio", "command": sys.executable, "args": [str(Path(__file__).with_name("mcp_sample_server.py"))]}


@pytest.fixture(autouse=True)
def fresh_cache():
    mcp.clear_cache()
    yield
    mcp.clear_cache()


async def test_tools_load_from_a_server_and_a_broken_server_is_skipped():
    broken = {"transport": "stdio", "command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}
    tools = await mcp.load_mcp_tools({"broken": broken, "sample": SAMPLE})
    assert sorted(t.name for t in tools) == ["add", "die", "explode"]
    assert await mcp.load_mcp_tools({"nope": {"transport": "bogus"}}) == []


async def test_tool_name_cannot_shadow_a_builtin():
    tools = await mcp.load_mcp_tools({"sample": SAMPLE}, reserved=frozenset({"add"}) | RESERVED_TOOL_NAMES)
    assert sorted(t.name for t in tools) == ["die", "explode"]


async def test_agent_calls_an_mcp_tool_and_survives_a_failing_one(persistence, new_turn):
    tools = await mcp.load_mcp_tools({"sample": SAMPLE})
    results = []

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            results.append((messages[-1].status, messages[-1].text))
            if len(results) == 1:
                return tool_call("explode", {}, "call-2")
            return AIMessage("done")
        return tool_call("add", {"a": 2, "b": 3})

    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=persistence.store, extra_tools=tools)
    _, _, context = new_turn()
    state, _, _ = await run_turn(agent, context, "add")
    assert state["messages"][-1].content == "done"
    assert results[0] == ("success", "5")
    assert results[1][0] == "error" and "kaboom" in results[1][1]


async def test_a_server_dying_mid_call_is_a_tool_error_not_a_crashed_run(persistence, new_turn):
    tools = await mcp.load_mcp_tools({"sample": SAMPLE})
    results = []

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            results.append((messages[-1].status, messages[-1].text))
            return AIMessage("done")
        return tool_call("die", {})

    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=persistence.store, extra_tools=tools)
    _, _, context = new_turn()
    state, _, _ = await run_turn(agent, context, "go")
    assert state["messages"][-1].content == "done"
    assert results[0][0] == "error"
