"""Runs the agent once against a real chat model + real sandbox, printing its tool calls and events.

    .venv\\Scripts\\python.exe scripts/smoke-agent.py [--model openai:gpt-5] [--prompt "..."]

Needs Postgres + Redis (docker compose up -d), the provider's API key, AGENT_ENGINE_NAME and the GCS ADC
(see CLAUDE.md). It creates a throwaway user and conversation and removes them (and the sandbox) afterwards.
If it fails with "server closed the connection unexpectedly", set DATABASE_URL/REDIS_URL to 127.0.0.1
in the shell: `localhost` can resolve to ::1, where Windows' wslrelay listens (see CLAUDE.md caveats).
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage  # noqa: E402

from app.agent.context import make_context  # noqa: E402
from app.agent.graph import build_agent, open_persistence, run_config  # noqa: E402
from app.agent.models import make_chat_model  # noqa: E402
from app.agent.sandbox import delete_sandbox_sync, make_sandbox_manager  # noqa: E402
from app.config import DEFAULT_MODEL  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.models_agent import Conversation  # noqa: E402
from app.redis_client import async_redis_client, redis_client  # noqa: E402
from app.services.storage import get_storage  # noqa: E402

DEFAULT_PROMPT = "Use python to compute the sum of the squares of 1..10 and tell me the result in one sentence."


async def main(model: str, prompt: str) -> None:
    db = SessionLocal()
    user = User(auth0_sub=f"smoke|{uuid.uuid4()}", email="smoke@example.invalid", name="smoke")
    db.add(user)
    db.flush()
    conv = Conversation(user_id=user.id, model=model)
    db.add(conv)
    db.commit()
    user_id, conv_id, run_id = user.id, conv.id, uuid.uuid4()
    print(f"model={model} conversation={conv_id}", flush=True)
    try:
        async with open_persistence() as p:
            agent = build_agent(make_chat_model(model), checkpointer=p.checkpointer, store=p.store)
            ctx = make_context(
                user_id=user_id,
                conversation_id=conv_id,
                run_id=run_id,
                manager=make_sandbox_manager(async_redis_client),
                storage=get_storage(),
            )
            async for mode, chunk in agent.astream(
                {"messages": [HumanMessage(prompt)]},
                config=run_config(conv_id, run_id),
                context=ctx,
                stream_mode=["messages", "updates", "custom"],
            ):
                if mode == "custom":
                    print("[custom]", chunk, flush=True)
                elif mode == "updates":
                    for node, update in chunk.items():
                        messages = update.get("messages", []) if isinstance(update, dict) else []
                        for m in messages:
                            print(f"[{node}] {m.type}: {m.text[:400]!r} tool_calls={m.tool_calls if m.type == 'ai' else None}", flush=True)
            await p.checkpointer.adelete_thread(str(conv_id))
    finally:
        db.expire_all()
        conv = db.get(Conversation, conv_id)
        if conv is not None and conv.sandbox_id:
            try:
                delete_sandbox_sync(conv.sandbox_id, redis_client)
            except Exception as exc:
                print("sandbox delete failed:", type(exc).__name__)
        db.query(Conversation).filter(Conversation.id == conv_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL, help="an ALLOWED_MODELS id (default: DEFAULT_MODEL)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = parser.parse_args()
    # psycopg's async mode needs a selector loop on Windows.
    asyncio.run(main(args.model, args.prompt), loop_factory=asyncio.SelectorEventLoop)
