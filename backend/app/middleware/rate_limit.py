import time

from fastapi import HTTPException

from app.redis import get_redis


async def enforce_rate_limit(key: str, limit: int, window_s: int = 60) -> None:
    if limit <= 0:
        return
    r = get_redis()
    bucket = f"rl:{key}:{int(time.time() // window_s)}"
    count = await r.incr(bucket)
    if count == 1:
        await r.expire(bucket, window_s + 10)
    if count > limit:
        raise HTTPException(status_code=429, detail="rate_limited")
