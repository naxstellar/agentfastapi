"""WP6A: long-term memory tools, prompt injection, per-user isolation and the /memories API."""

import json

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from sqlalchemy import text

from app.agent import memory as memory_module
from app.agent.graph import build_agent
from app.agent.memory import memory_namespace
from tests.fakes import ScriptedChatModel
from tests.test_agent_graph import humans, persistence, run_turn, tool_call  # noqa: F401
from tests.test_agent_graph import manager, new_turn, provider, session_factory, storage  # noqa: F401

CSRF = {"X-Requested-With": "fetch"}


def system_text(messages) -> str:
    return "\n".join(m.text for m in messages if isinstance(m, SystemMessage))


def saver(text):
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(f"tool said: {messages[-1].content}")
        return tool_call("save_memory", {"text": text})

    return respond


async def test_memory_saved_in_one_conversation_is_in_the_prompt_of_the_next(persistence, new_turn):
    agent = build_agent(ScriptedChatModel(responder=saver("Prefers charts in dark mode")), checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, context = new_turn()
    state, _, _ = await run_turn(agent, context, "remember my preference")
    assert state["messages"][-1].content == "tool said: Saved."

    seen = []

    def respond(messages):
        seen.append(system_text(messages))
        return AIMessage("ok")

    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, second = new_turn()  # a different conversation, same user
    await run_turn(agent, second, "hello")
    assert "Prefers charts in dark mode" in seen[0]


async def test_memory_is_never_visible_to_another_user(persistence, new_turn, make_user):
    store = persistence.store
    other = make_user("auth0|other-memory")
    await store.aput(memory_namespace(other.id), "k1", {"text": "other user's secret"})

    seen = []

    def respond(messages):
        seen.append(system_text(messages))
        if isinstance(messages[-1], ToolMessage):
            seen.append(messages[-1].content)
            return AIMessage("done")
        return tool_call("recall_memory", {})

    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=store)
    _, _, context = new_turn()
    await run_turn(agent, context, "what do you know about me?")
    assert not any("secret" in s for s in seen)
    assert "Nothing remembered." in seen


async def test_recall_filters_by_query_and_save_deduplicates(persistence, new_turn):
    store = persistence.store
    _, _, context = new_turn()
    ns = memory_namespace(context.user_id)
    await store.aput(ns, "a", {"text": "Fiscal year starts in April"})
    await store.aput(ns, "b", {"text": "Prefers bar charts"})

    results = []

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            results.append(messages[-1].content)
            if len(results) == 1:
                return tool_call("save_memory", {"text": "prefers BAR charts"}, "call-2")
            return AIMessage("done")
        return tool_call("recall_memory", {"query": "fiscal"})

    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=store)
    await run_turn(agent, context, "go")
    assert results == ["- Fiscal year starts in April", "Already remembered."]


async def test_memory_count_and_length_caps(persistence, new_turn, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_MEMORIES", 1)
    monkeypatch.setattr(memory_module, "MAX_MEMORY_CHARS", 20)
    results = []
    texts = iter(["one", "two", "x" * 21])

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            results.append(messages[-1].content)
        try:
            return tool_call("save_memory", {"text": next(texts)}, f"call-{len(results)}")
        except StopIteration:
            return AIMessage("done")

    agent = build_agent(ScriptedChatModel(responder=respond), checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, context = new_turn()
    await run_turn(agent, context, "go")
    assert results[0] == "Saved."
    assert results[1].startswith("Error: the memory is full")
    assert results[2].startswith("Error: too long")


def put_row(db, user, key, text_):
    db.execute(
        text("INSERT INTO store (prefix, key, value) VALUES (:p, :k, CAST(:v AS jsonb))"),
        {"p": ".".join(memory_namespace(user.id)), "k": key, "v": json.dumps({"text": text_})},
    )


async def test_memories_api_lists_and_deletes_only_own(authed_client, db, user, make_user):
    # The API reads the store's table directly; create it in the test schema (rolled back with the test).
    db.execute(text("CREATE TABLE store (prefix text NOT NULL, key text NOT NULL, value jsonb NOT NULL, "
                    "created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), PRIMARY KEY (prefix, key))"))
    other = make_user("auth0|other-memory")
    put_row(db, user, "mine", "likes pivot tables")
    put_row(db, other, "theirs", "not yours")

    listed = (await authed_client.get("/api/v1/memories")).json()
    assert [(m["id"], m["text"]) for m in listed] == [("mine", "likes pivot tables")]

    assert (await authed_client.delete("/api/v1/memories/theirs", headers=CSRF)).status_code == 404
    assert (await authed_client.delete("/api/v1/memories/mine", headers=CSRF)).status_code == 204
    assert (await authed_client.get("/api/v1/memories")).json() == []
    assert db.execute(text("SELECT count(*) FROM store")).scalar() == 1


async def test_memories_api_without_the_store_table_is_empty(authed_client, user):
    assert (await authed_client.get("/api/v1/memories")).json() == []
