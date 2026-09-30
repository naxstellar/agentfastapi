import redis
import redis.asyncio

from app.config import REDIS_URL

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# For async code in the API process (e.g. SSE). Celery workers create their own on their event loop.
async_redis_client = redis.asyncio.Redis.from_url(REDIS_URL, decode_responses=True)
