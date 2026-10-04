"""Token metering for a run: a LangChain callback that counts every LLM call (the agent's and the summarizer's,
since callbacks in the run config reach both) against the user's quota and publishes a `usage` event."""

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import LLMResult

from app.services import events
from app.services.quota import Quota

logger = logging.getLogger("app.agent.usage")


def tokens_from_result(result: LLMResult) -> tuple[int, int]:
    """(input, output) tokens of one LLM call. `usage_metadata` is normalized across providers (Anthropic cache
    tokens are already in `input_tokens`); OpenAI's raw `token_usage` is the fallback."""
    input_tokens = output_tokens = 0
    for generations in result.generations:
        for generation in generations:
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if usage:
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
    if not (input_tokens or output_tokens):
        raw = (result.llm_output or {}).get("token_usage") or {}
        input_tokens = raw.get("prompt_tokens", 0) or raw.get("input_tokens", 0)
        output_tokens = raw.get("completion_tokens", 0) or raw.get("output_tokens", 0)
    return int(input_tokens), int(output_tokens)


@dataclass
class _Call:
    """What an in-flight LLM call has consumed so far, for the case it never reports usage."""

    messages: list[BaseMessage]
    partial: list[str] = field(default_factory=list)


def _estimate(call: _Call) -> tuple[int, int]:
    """(input, output) tokens of a call that never reported usage: the prompt and the output streamed so far."""
    return count_tokens_approximately(call.messages), math.ceil(sum(len(part) for part in call.partial) / 4)


class UsageCallback(AsyncCallbackHandler):
    """Counts each LLM call when it ends. A call that is cancelled or fails mid-way reports no usage but the
    provider still bills it, so those are charged an estimate (see `on_llm_error` and `charge_unfinished`)."""

    def __init__(self, quota: Quota, events_client: redis.asyncio.Redis, user_id: uuid.UUID, run_id: uuid.UUID, day_limit: int):
        self._quota, self._events, self._user_id, self._run_id, self._limit = quota, events_client, user_id, run_id, day_limit
        self._calls: dict[uuid.UUID, _Call] = {}

    async def on_chat_model_start(self, serialized: dict, messages: list[list[BaseMessage]], *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._calls[run_id] = _Call(messages[0] if messages else [])

    async def on_llm_new_token(self, token: str, *, chunk: Any = None, run_id: uuid.UUID, **kwargs: Any) -> None:
        call = self._calls.get(run_id)
        if call is None:
            return
        call.partial.append(token if isinstance(token, str) else "")
        message = getattr(chunk, "message", None)  # tool-call arguments stream separately from the text
        for tool_chunk in getattr(message, "tool_call_chunks", None) or []:
            call.partial.append(tool_chunk.get("args") or "")

    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._calls.pop(run_id, None)
        await self._record(*tokens_from_result(response))

    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        call = self._calls.pop(run_id, None)
        if call is not None:
            await self._record(*_estimate(call))

    async def charge_unfinished(self) -> None:
        """Charges the calls still open when the run's stream ended. A cancelled run (or one whose graph is torn
        down) never gets `on_llm_error` for the call it interrupted, so the runner calls this in its `finally`."""
        calls, self._calls = list(self._calls.values()), {}
        for call in calls:
            await self._record(*_estimate(call))

    async def _record(self, input_tokens: int, output_tokens: int) -> None:
        if not (input_tokens or output_tokens):
            return
        try:
            run_in, run_out, day_used = await asyncio.to_thread(
                self._quota.add_usage, self._user_id, self._run_id, input_tokens, output_tokens
            )
            await events.publish(
                self._events,
                self._run_id,
                "usage",
                {"input_tokens": run_in, "output_tokens": run_out, "day_used": day_used, "day_limit": self._limit},
            )
        except Exception as exc:  # metering must never fail the run (this call's tokens go uncounted)
            logger.warning("Counting usage for run id=%s failed: %s", self._run_id, type(exc).__name__)
