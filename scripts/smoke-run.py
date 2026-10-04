"""Enqueues one run through Celery and prints its event stream: the real worker path end to end.

    .venv\\Scripts\\python.exe scripts/smoke-run.py [--model openai:gpt-5] [--prompt "..."] [--cancel-after 3]

Needs Postgres + Redis, a running worker (scripts/start-worker.ps1) and what smoke-agent.py needs (provider key,
AGENT_ENGINE_NAME, GCS ADC). It creates a throwaway user and conversation and removes them afterwards.
With --cancel-after N it requests cancellation N seconds in, to check the cancel path.
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.history import add_user_message  # noqa: E402
from app.agent.sandbox import delete_sandbox_sync  # noqa: E402
from app.config import DEFAULT_MODEL  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.models_agent import Conversation, Run  # noqa: E402
from app.redis_client import redis_client  # noqa: E402
from app.services import events, run_guard  # noqa: E402
from app.workers.runner import cancel_key  # noqa: E402
from app.workers.tasks import run_agent  # noqa: E402

DEFAULT_PROMPT = "Use python to compute the sum of the squares of 1..10 and tell me the result in one sentence."


async def main(model: str, prompt: str, cancel_after: float | None) -> None:
    db = SessionLocal()
    user = User(auth0_sub=f"smoke|{uuid.uuid4()}", email="smoke@example.invalid", name="smoke")
    db.add(user)
    db.flush()
    conv = Conversation(user_id=user.id, model=model)
    db.add(conv)
    db.flush()
    run = Run(conversation_id=conv.id, user_id=user.id, status="queued")
    db.add(run)
    db.flush()
    add_user_message(db, conv.id, run.id, prompt)
    db.commit()
    user_id, conv_id, run_id = user.id, conv.id, run.id
    client = events.make_events_client()
    try:
        assert run_guard.acquire(redis_client, user_id, conv_id, run_id) is run_guard.Admission.OK
        run_agent.apply_async(args=[str(run_id)], task_id=str(run_id))
        print(f"model={model} run={run_id}", flush=True)
        if cancel_after is not None:
            asyncio.get_running_loop().call_later(
                cancel_after, lambda: asyncio.ensure_future(client.set(cancel_key(run_id), "1", ex=600))
            )
        async with asyncio.timeout(300):
            async for item in events.tail(client, run_id):
                if item is not None:
                    _, kind, data = item
                    print(f"[{kind}]", str(data)[:300], flush=True)
        db.expire_all()
        print("final status:", db.get(Run, run_id).status)
    finally:
        db.expire_all()
        conv = db.get(Conversation, conv_id)
        if conv is not None and conv.sandbox_id:
            try:
                delete_sandbox_sync(conv.sandbox_id, redis_client)
            except Exception as exc:
                print("sandbox delete failed:", type(exc).__name__)
        run_guard.release(redis_client, user_id, conv_id, run_id)
        await client.delete(events.stream_key(run_id))
        await client.aclose()
        db.query(Conversation).filter(Conversation.id == conv_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--cancel-after", type=float, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.model, args.prompt, args.cancel_after))
