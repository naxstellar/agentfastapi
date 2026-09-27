from app.redis_client import redis_client

STATE_TTL_SECONDS = 300


def save_state(state: str) -> None:
    redis_client.setex(f"auth0_state:{state}", STATE_TTL_SECONDS, "1")


def consume_state(state: str) -> bool:
    key = f"auth0_state:{state}"
    pipe = redis_client.pipeline()
    pipe.get(key)
    pipe.delete(key)
    exists, _ = pipe.execute()
    return exists is not None
