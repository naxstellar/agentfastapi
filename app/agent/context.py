"""Per-run context handed to the agent's tools and middleware, and the run's side-channel events.

Identity lives here, never in tool arguments: the LLM can't choose which user's or conversation's data
a tool touches. The context isn't checkpointed; the runner passes it on every invocation and resume.
"""

import uuid
from dataclasses import dataclass

from langgraph.config import get_stream_writer

from app.agent.sandbox import SandboxManager
from app.agent.workspace import ConversationWorkspace, SessionFactory
from app.db import SessionLocal
from app.services.storage import Storage


@dataclass(frozen=True)
class AgentContext:
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    workspace: ConversationWorkspace  # one per run: it serializes the run's sandbox executions


def emit(event_type: str, data: dict) -> None:
    """Publishes a §2.4 event (status, artifact, ...) from inside the graph.

    It travels on LangGraph's "custom" stream mode as {"type": ..., "data": ...}, in order with the
    token and update chunks; the runner maps it to the run's event stream. A no-op outside a graph run
    or when the caller didn't ask for "custom".
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:  # not inside a runnable
        return
    writer({"type": event_type, "data": data})


async def _emit_async(event_type: str, data: dict) -> None:
    emit(event_type, data)


def make_context(
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    manager: SandboxManager,
    storage: Storage,
    session_factory: SessionFactory = SessionLocal,
) -> AgentContext:
    workspace = ConversationWorkspace(
        user_id=user_id,
        conversation_id=conversation_id,
        run_id=run_id,
        manager=manager,
        storage=storage,
        session_factory=session_factory,
        emit=_emit_async,
    )
    return AgentContext(user_id, conversation_id, run_id, workspace)
