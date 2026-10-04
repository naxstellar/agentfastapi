"""Display history in the `messages` table. The checkpoint is the agent's source of truth; this is what
the UI shows (GET /conversations/{id}/messages).

Content shapes (JSONB):
- user:      {"run_id", "text"}
- assistant: {"run_id", "text", "tools": [{"name", "ok"}], "artifacts": [{"id", "kind", "filename"}]}
"""

import uuid
from collections.abc import Iterable

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_agent import Message

# create_agent's node names; middleware nodes ("<Middleware>.before_model", ...) rewrite history
# (summarization) rather than add to the turn.
_TURN_NODES = ("model", "tools")


class TurnRecorder:
    """Collects the messages a run adds, from graph.astream(stream_mode="updates") chunks."""

    def __init__(self) -> None:
        self.messages: list[BaseMessage] = []

    def add_update(self, update: dict) -> None:
        for node, values in update.items():
            if not isinstance(values, dict):
                continue
            if node in _TURN_NODES:
                self.messages.extend(m for m in values.get("messages", []) if isinstance(m, (AIMessage, ToolMessage)))
            elif node.endswith(".after_model"):
                # The approval step re-emits the (maybe edited) AI message, which the model node already gave us;
                # only a rejection's tool result is new.
                self.messages.extend(m for m in values.get("messages", []) if isinstance(m, ToolMessage))

    def content(self) -> dict:
        return assistant_content(self.messages)


def assistant_content(messages: Iterable[BaseMessage]) -> dict:
    """The display form of a run's AI and tool messages: all assistant text, plus a compact tool summary."""
    texts, tools, artifacts = [], [], []
    for message in messages:
        if isinstance(message, AIMessage):
            if text := message.text.strip():
                texts.append(text)
        elif isinstance(message, ToolMessage):
            tools.append({"name": message.name, "ok": message.status != "error"})
            if isinstance(message.artifact, dict):
                artifacts.extend(message.artifact.get("artifacts", []))
    return {"text": "\n\n".join(texts), "tools": tools, "artifacts": artifacts}


def add_user_message(db: Session, conversation_id: uuid.UUID, run_id: uuid.UUID, text: str) -> Message:
    """Called by the API when it creates the run, in the same transaction. The caller commits."""
    message = Message(conversation_id=conversation_id, role="user", content={"run_id": str(run_id), "text": text})
    db.add(message)
    return message


def save_assistant_message(db: Session, conversation_id: uuid.UUID, run_id: uuid.UUID, content: dict) -> bool:
    """Idempotent per run: a redelivered or resumed run doesn't add a second reply. Returns whether it added one.

    The caller commits.
    """
    existing = db.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
            Message.content["run_id"].astext == str(run_id),
        )
    )
    if existing is not None:
        return False
    db.add(Message(conversation_id=conversation_id, role="assistant", content={"run_id": str(run_id), **content}))
    return True
