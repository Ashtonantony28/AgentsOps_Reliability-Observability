from fastapi import HTTPException, Request

from app.redis import get_redis


async def enforce_rate_limit(key: str, limit_per_min: int) -> None:
    if limit_per_min <= 0:
        return
    r = get_redis()
    bucket = f"rl:{key}:{int(__import__('time').time() // 60)}"
    count = await r.incr(bucket)
    if count == 1:
        await r.expire(bucket, 70)
    if count > limit_per_min:
        raise HTTPException(status_code=429, detail="rate_limited")
