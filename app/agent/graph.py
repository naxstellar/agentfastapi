"""The data-analytics agent: LangChain v1 `create_agent` with Postgres-backed state.

The checkpointer keeps each conversation's message state (thread_id = conversation id); the store holds
per-user long-term memory (WP6). Both share one psycopg3 async pool, opened once per worker process.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.context import AgentContext, emit
from app.agent.mcp import McpToolErrors
from app.agent.memory import MEMORY_TOOLS, MemoryMiddleware
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.config import AGENT_RECURSION_LIMIT, DATABASE_URL, SUMMARIZE_KEEP_MESSAGES, SUMMARIZE_TRIGGER_TOKENS


class AgentSummarization(SummarizationMiddleware):
    """Summarizes older turns once the history passes `trigger_tokens`, keeping recent ones verbatim.

    Two changes to the stock middleware:
    - It emits a `status` event before the (slow) summary call.
    - Besides the last `keep_messages`, it keeps at most half the trigger in tokens. A count alone can
      keep the history over the trigger (20 large tool outputs can be ~100k tokens), and then every
      model call would summarize again.
    """

    def __init__(self, model: BaseChatModel, *, trigger_tokens: int, keep_messages: int):
        super().__init__(
            model,
            trigger=("tokens", trigger_tokens),
            keep=("messages", keep_messages),
            # The stock default (4000) shows the summarizer only the last 4000 tokens of what it replaces.
            trim_tokens_to_summarize=trigger_tokens,
        )
        self.keep_tokens = trigger_tokens // 2

    def _determine_cutoff_index(self, messages: list[AnyMessage]) -> int:
        cutoff = super()._determine_cutoff_index(messages)
        if not messages:
            return cutoff
        # The newest message (usually the user's question) is always kept.
        kept = self.token_counter([messages[-1]])
        for index in range(len(messages) - 2, cutoff - 1, -1):
            kept += self.token_counter([messages[index]])
            if kept > self.keep_tokens:
                # Keep messages[index + 1:], moved back if needed so tool calls stay with their results.
                return max(cutoff, self._find_safe_cutoff_point(messages, index + 1))
        return cutoff

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        emit("status", {"message": "Summarizing earlier messages…"})
        return await super()._acreate_summary(messages_to_summarize)


# Names an MCP tool may not take: the built-in tools and the memory tools.
RESERVED_TOOL_NAMES = frozenset(t.name for t in [*TOOLS, *MEMORY_TOOLS])


def build_agent(
    model: BaseChatModel,
    *,
    checkpointer: AsyncPostgresSaver | None = None,
    store: AsyncPostgresStore | None = None,
    summary_model: BaseChatModel | None = None,
    extra_tools: Sequence[BaseTool] = (),
    require_approval: bool = False,
):
    """Cheap to call: build one per run with the conversation's model.

    `require_approval` pauses the run before every `run_python` call for the user's decision (WP6C). A resumed
    run must be built with it on, whatever the user's setting is now: the pause is part of its checkpoint.
    """
    middleware = [
        MemoryMiddleware(),
        AgentSummarization(
            summary_model or model,
            trigger_tokens=SUMMARIZE_TRIGGER_TOKENS,
            keep_messages=SUMMARIZE_KEEP_MESSAGES,
        ),
    ]
    if extra_tools:
        middleware.append(McpToolErrors(list(extra_tools)))
    if require_approval:
        middleware.append(
            HumanInTheLoopMiddleware(
                interrupt_on={"run_python": {"allowed_decisions": ["approve", "edit", "reject"]}},
                description_prefix="The agent wants to run this code",
            )
        )
    return create_agent(
        model,
        tools=[*TOOLS, *extra_tools],
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        context_schema=AgentContext,
        checkpointer=checkpointer,
        store=store,
        name="data_analyst",
    )


def run_config(conversation_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    return {
        "configurable": {"thread_id": str(conversation_id)},
        "recursion_limit": AGENT_RECURSION_LIMIT,
        "metadata": {"run_id": str(run_id)},
    }


def psycopg_conninfo(database_url: str) -> str:
    """DATABASE_URL is an SQLAlchemy URL (postgresql+psycopg2://...); psycopg3 wants plain postgresql://."""
    scheme, sep, rest = database_url.partition("://")
    return f"{scheme.partition('+')[0]}{sep}{rest}"


@dataclass(frozen=True)
class Persistence:
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore


@asynccontextmanager
async def open_persistence(
    conninfo: str | None = None, *, max_size: int = 10, connect_kwargs: dict | None = None
) -> AsyncIterator[Persistence]:
    """The checkpointer and store on one pool; creates/migrates their tables (idempotent).

    Must run on a selector event loop on Windows: psycopg's async mode doesn't support the default
    Proactor loop.
    """
    pool = AsyncConnectionPool(
        conninfo or psycopg_conninfo(DATABASE_URL),
        min_size=1,
        max_size=max_size,
        # Required by both: autocommit for setup()'s DDL, dict rows for their queries, and no server-side
        # prepared statements (they break behind PgBouncer in transaction mode).
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row, **(connect_kwargs or {})},
        open=False,
    )
    async with pool:
        checkpointer = AsyncPostgresSaver(pool)
        store = AsyncPostgresStore(pool)
        await checkpointer.setup()
        await store.setup()
        yield Persistence(checkpointer, store)
