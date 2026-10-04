"""WP3 acceptance: the agent graph with a scripted chat model, the fake sandbox and the real Postgres
checkpointer/store (in a throwaway schema)."""

import uuid
from contextlib import contextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

import app.agent.graph as graph_module
from app.agent.context import emit, make_context
from app.agent.graph import AgentSummarization, build_agent, open_persistence, psycopg_conninfo, run_config
from app.agent.history import TurnRecorder
from app.agent.sandbox import SandboxManager, SandboxSlots
from app.config import DATABASE_URL
from app.models_agent import Conversation, ConversationDataset, Dataset, Run
from tests.fakes import FakeSandboxProvider, FakeStorage, ScriptedChatModel

# --- fixtures -------------------------------------------------------------------------------------------


async def no_sleep(_):
    pass


@pytest.fixture
async def persistence(fresh_schema):
    async with open_persistence(
        psycopg_conninfo(DATABASE_URL), max_size=2, connect_kwargs={"options": f"-csearch_path={fresh_schema}"}
    ) as p:
        yield p


@pytest.fixture
def provider():
    return FakeSandboxProvider()


@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def manager(fake_async_redis, provider):
    return SandboxManager(
        provider, SandboxSlots(fake_async_redis, 5), ttl_s=3600, idle_timeout_s=300, exec_timeout_s=5, sleep=no_sleep
    )


@pytest.fixture
def session_factory(db):
    @contextmanager
    def factory():
        yield db

    return factory


@pytest.fixture
def new_turn(db, user, manager, storage, session_factory):
    """A conversation plus a run in it; returns (conversation, run, context)."""

    def _new(conversation=None):
        if conversation is None:
            conversation = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
            db.add(conversation)
            db.flush()
        run = Run(conversation_id=conversation.id, user_id=user.id, status="running")
        db.add(run)
        db.commit()
        context = make_context(
            user_id=user.id,
            conversation_id=conversation.id,
            run_id=run.id,
            manager=manager,
            storage=storage,
            session_factory=session_factory,
        )
        return conversation, run, context

    return _new


async def run_turn(agent, context, text_or_messages):
    """Streams one run like the worker will; returns (final state, custom events, recorder)."""
    messages = [HumanMessage(text_or_messages)] if isinstance(text_or_messages, str) else text_or_messages
    events, recorder = [], TurnRecorder()
    async for mode, chunk in agent.astream(
        {"messages": messages},
        config=run_config(context.conversation_id, context.run_id),
        context=context,
        stream_mode=["updates", "custom"],
    ):
        if mode == "custom":
            events.append((chunk["type"], chunk["data"]))
        else:
            recorder.add_update(chunk)
    state = await agent.aget_state(run_config(context.conversation_id, context.run_id))
    return state.values, events, recorder


def humans(messages):
    return [m.content for m in messages if isinstance(m, HumanMessage)]


def tool_call(name, args, call_id="call-1"):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def is_summary_request(messages):
    return len(messages) == 1 and "Messages to summarize" in messages[0].text


def code_then_answer(messages):
    """Runs code once per user message, then answers with the tool output."""
    if is_summary_request(messages):
        return AIMessage("SUMMARY of earlier turns")
    last = messages[-1]
    if isinstance(last, HumanMessage):
        return tool_call("run_python", {"code": f"print({len(humans(messages))})"})
    if isinstance(last, ToolMessage):
        return AIMessage(f"Done. Tool said: {last.content}")
    return AIMessage("?")


# --- tool loop and state --------------------------------------------------------------------------------


async def test_tool_loop_completes(persistence, new_turn, provider):
    model = ScriptedChatModel(responder=code_then_answer)
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    conversation, run, context = new_turn()

    state, events, recorder = await run_turn(agent, context, "How many rows?")

    assert [type(m).__name__ for m in state["messages"]] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    tool_message = state["messages"][2]
    assert tool_message.name == "run_python" and tool_message.status == "success"
    assert "ran: print(1)" in tool_message.content
    assert state["messages"][-1].content.startswith("Done. Tool said: stdout:\nran: print(1)")
    assert provider.created[0].executed == ["print(1)"]
    # The workspace's sandbox status reaches the run's custom stream.
    assert ("status", {"message": "Starting sandbox…"}) in events
    assert recorder.content() == {
        "text": state["messages"][-1].content,
        "tools": [{"name": "run_python", "ok": True}],
        "artifacts": [],
    }
    # The system prompt is sent, and the tools' runtime argument is hidden from the model.
    assert "data/" in model.calls[0][0].content


async def test_state_persists_across_runs_in_a_conversation(persistence, new_turn, provider):
    model = ScriptedChatModel(responder=code_then_answer)
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    conversation, _, first = new_turn()
    await run_turn(agent, first, "first question")

    _, _, second = new_turn(conversation)
    state, _, recorder = await run_turn(agent, second, "second question")

    assert humans(state["messages"]) == ["first question", "second question"]
    assert humans(model.calls[-2]) == ["first question", "second question"]
    assert len(state["messages"]) == 8
    # Only the second run's messages are recorded for its display reply.
    assert len(recorder.messages) == 3
    assert len(provider.created) == 1 and provider.created[0].executed == ["print(1)", "print(2)"]


async def test_conversations_do_not_share_state(persistence, new_turn, provider):
    model = ScriptedChatModel(responder=code_then_answer)
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, a = new_turn()
    _, _, b = new_turn()

    await run_turn(agent, a, "question in A")
    state_b, _, _ = await run_turn(agent, b, "question in B")

    assert humans(state_b["messages"]) == ["question in B"]
    assert all("question in A" not in m.text for call in model.calls[2:] for m in call)
    # Each conversation gets its own sandbox.
    assert len(provider.created) == 2


# --- dataset tools --------------------------------------------------------------------------------------


@pytest.fixture
def attach_dataset(db, storage):
    def _attach(owner, conversation, filename, sheets):
        dataset = Dataset(
            user_id=owner.id,
            filename=filename,
            gcs_key=f"users/{owner.id}/datasets/{uuid.uuid4()}/{filename}",
            size_bytes=2048,
            content_type="text/csv",
            status="ready",
            sheets=sheets,
        )
        db.add(dataset)
        db.flush()
        storage.objects[dataset.gcs_key] = b"x" * 2048
        db.add(ConversationDataset(conversation_id=conversation.id, dataset_id=dataset.id))
        db.commit()
        return dataset

    return _attach


async def test_dataset_tools(persistence, new_turn, attach_dataset, user, provider):
    sheets = [{"name": "Q3", "columns": ["region", "revenue"], "row_count": 120, "rows_truncated": False}]

    def responder(messages):
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if len(tool_messages) == 0:
            return tool_call("list_datasets", {})
        if len(tool_messages) == 1:
            return tool_call("inspect_dataset", {"name": "Sales Q3.xlsx", "sheet": "Q3", "rows": 500}, "call-2")
        if len(tool_messages) == 2:
            return tool_call("inspect_dataset", {"name": "missing.csv"}, "call-3")
        return AIMessage("ok")

    model = ScriptedChatModel(responder=responder)
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    conversation, _, context = new_turn()
    attach_dataset(user, conversation, "Sales Q3.xlsx", sheets)

    state, _, recorder = await run_turn(agent, context, "look at my data")

    listed, inspected, missing = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert listed.content == (
        "data/Sales_Q3.xlsx (uploaded as 'Sales Q3.xlsx', 2.0 KB)\n"
        "  sheet 'Q3': 120 data rows; columns: 'region', 'revenue'"
    )
    # The dataset was hydrated, then the inspection ran against its sandbox path with clamped rows.
    sandbox = provider.created[0]
    assert "data/Sales_Q3.xlsx" in sandbox.files
    inspect_code = [c for c in sandbox.executed if "_inspect_dataset(" in c]
    assert len(inspect_code) == 1 and "_inspect_dataset('data/Sales_Q3.xlsx', 'Q3', 50)" in inspect_code[0]
    assert inspected.status == "success"
    assert missing.status == "error" and "no attached dataset named 'missing.csv'" in missing.content
    assert recorder.content()["tools"] == [
        {"name": "list_datasets", "ok": True},
        {"name": "inspect_dataset", "ok": True},
        {"name": "inspect_dataset", "ok": False},
    ]


async def test_list_datasets_without_attachments(persistence, new_turn):
    model = ScriptedChatModel(
        responder=lambda m: AIMessage("ok") if isinstance(m[-1], ToolMessage) else tool_call("list_datasets", {})
    )
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, context = new_turn()

    state, _, _ = await run_turn(agent, context, "what data do I have?")

    assert "No datasets are attached" in state["messages"][2].content


# --- summarization --------------------------------------------------------------------------------------


@pytest.fixture
def small_summary_threshold(monkeypatch):
    monkeypatch.setattr(graph_module, "SUMMARIZE_TRIGGER_TOKENS", 2000)
    monkeypatch.setattr(graph_module, "SUMMARIZE_KEEP_MESSAGES", 6)
    return 2000


def chat_history(turns, chars):
    history = []
    for i in range(turns):
        history += [HumanMessage(f"question {i} " + "q" * chars), AIMessage(f"answer {i} " + "a" * chars)]
    return history


async def test_long_history_is_summarized_below_the_threshold(persistence, new_turn, monkeypatch):
    threshold = 6000
    monkeypatch.setattr(graph_module, "SUMMARIZE_TRIGGER_TOKENS", threshold)
    monkeypatch.setattr(graph_module, "SUMMARIZE_KEEP_MESSAGES", 6)
    model = ScriptedChatModel(
        responder=lambda m: AIMessage("SUMMARY of earlier turns") if is_summary_request(m) else AIMessage("final")
    )
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, context = new_turn()
    history = chat_history(16, 800)  # ~6600 tokens: just past the trigger, as it happens turn by turn
    assert count_tokens_approximately(history) > threshold

    state, events, _ = await run_turn(agent, context, [*history, HumanMessage("latest question")])

    assert ("status", {"message": "Summarizing earlier messages…"}) in events
    summary_calls = [c for c in model.calls if is_summary_request(c)]
    assert len(summary_calls) == 1
    # The summarizer saw the whole replaced part (over 4000 tokens, which the stock middleware would trim).
    assert "question 0 " in summary_calls[0][0].text
    messages = state["messages"]
    assert "SUMMARY of earlier turns" in messages[0].text
    assert humans(messages)[-1] == "latest question"
    assert count_tokens_approximately(messages) < threshold
    agent_call = model.calls[-1]
    assert count_tokens_approximately(agent_call) < threshold + 2000  # + system prompt


async def test_summarization_also_bounds_large_recent_messages(persistence, new_turn, small_summary_threshold):
    """Six kept messages of large tool output would stay over the trigger; tokens bound what is kept."""
    model = ScriptedChatModel(
        responder=lambda m: AIMessage("SUMMARY") if is_summary_request(m) else AIMessage("final")
    )
    agent = build_agent(model, checkpointer=persistence.checkpointer, store=persistence.store)
    _, _, context = new_turn()
    history = [HumanMessage("analyse this")]
    for i in range(4):
        history += [
            tool_call("run_python", {"code": f"step {i}"}, f"call-{i}"),
            ToolMessage("x" * 3200, tool_call_id=f"call-{i}", name="run_python"),  # ~800 tokens each
        ]

    state, _, _ = await run_turn(agent, context, [*history, HumanMessage("and now?")])

    messages = state["messages"]
    assert count_tokens_approximately(messages) < small_summary_threshold
    # Tool results are never kept without the AI message that called them.
    kept_call_ids = {c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    assert all(m.tool_call_id in kept_call_ids for m in messages if isinstance(m, ToolMessage))
    assert humans(messages)[-1] == "and now?"


def test_cutoff_keeps_newest_message_and_tool_pairs():
    middleware = AgentSummarization(ScriptedChatModel(responder=lambda m: AIMessage("")), trigger_tokens=200, keep_messages=4)
    messages = [
        HumanMessage("a" * 400),
        tool_call("run_python", {"code": "x"}, "c1"),
        ToolMessage("y" * 400, tool_call_id="c1"),
        HumanMessage("z" * 4000),  # alone over the token budget
    ]
    # Count-based cutoff alone would be 0 (4 messages <= keep); the token budget moves it, but the
    # newest message is always kept.
    assert middleware._determine_cutoff_index(messages) == 3


def test_emit_outside_a_graph_is_a_noop():
    emit("status", {"message": "nobody listening"})
