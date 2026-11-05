"""لایه رویداد و هماهنگی روی Redis.

- `publish/subscribe`: انتشار زنده رویدادها به پنل (Pub/Sub)
- `enqueue_run/consume_runs`: صف پایدار اجرای Runها (Redis Streams + Consumer Group)
- `acquire_lease/release_lease`: قفل اتمیک با TTL برای جلوگیری از اجرای هم‌زمان
- پرچم‌های سراسری: توقف اضطراری (STOP_ALL) و تحویل کنترل دسکتاپ (Takeover)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as aioredis

from .config import get_settings

EVENTS_CHANNEL = "xagent:events"
RUN_STREAM = "xagent:runs"
RUN_GROUP = "workers"
INBOX_STREAM = "xagent:inbox"
STOP_ALL_KEY = "xagent:stop_all"
TAKEOVER_KEY = "xagent:desktop:takeover"
RUN_CANCEL_PREFIX = "xagent:run:cancel:"

_redis: aioredis.Redis | None = None
_blocking: aioredis.Redis | None = None

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""
_EXTEND_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
else
  return 0
end
"""


def get_redis() -> aioredis.Redis:
    """اتصال عادی با مهلت معقول برای عملیات کوتاه."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            get_settings().redis_url, decode_responses=True, health_check_interval=30,
            socket_timeout=15, socket_connect_timeout=5,
        )
    return _redis


def get_blocking_redis() -> aioredis.Redis:
    """اتصال جداگانه بدون مهلت خواندن برای XREADGROUP مسدودکننده و Pub/Sub طولانی."""
    global _blocking
    if _blocking is None:
        _blocking = aioredis.from_url(
            get_settings().redis_url, decode_responses=True, health_check_interval=30,
            socket_timeout=None, socket_connect_timeout=5, socket_keepalive=True,
        )
    return _blocking


async def close_redis() -> None:
    global _redis, _blocking
    for c in (_redis, _blocking):
        if c is not None:
            await c.aclose()
    _redis = None
    _blocking = None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ── رویدادهای زنده ──
async def publish(event_type: str, data: dict[str, Any]) -> None:
    try:
        await get_redis().publish(EVENTS_CHANNEL, _dumps({"type": event_type, "data": data}))
    except Exception:  # انتشار زنده نباید هرگز منطق اصلی را بشکند
        pass


async def subscribe(ready: asyncio.Event | None = None) -> AsyncIterator[dict[str, Any]]:
    """ready: پس از اشتراک موفق تنظیم می‌شود (تشخیص بازیابی پمپ SSE پس از قطع Redis)."""
    pubsub = get_blocking_redis().pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)
    if ready is not None:
        ready.set()
    try:
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                try:
                    yield json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
    finally:
        await pubsub.unsubscribe(EVENTS_CHANNEL)
        await pubsub.aclose()


# ── صف Runها ──
async def ensure_groups() -> None:
    r = get_redis()
    for stream in (RUN_STREAM, INBOX_STREAM):
        try:
            await r.xgroup_create(stream, RUN_GROUP, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise


async def enqueue_run(run_id: uuid.UUID | str, reason: str = "new") -> None:
    await get_redis().xadd(RUN_STREAM, {"run_id": str(run_id), "reason": reason}, maxlen=10000, approximate=True)


async def enqueue_inbox(event_id: uuid.UUID | str) -> None:
    await get_redis().xadd(INBOX_STREAM, {"event_id": str(event_id)}, maxlen=10000, approximate=True)


# ── قفل‌های اتمیک ──
async def acquire_lease(name: str, ttl_ms: int, owner: str | None = None) -> str | None:
    token = owner or uuid.uuid4().hex
    ok = await get_redis().set(f"xagent:lease:{name}", token, nx=True, px=ttl_ms)
    return token if ok else None


async def extend_lease(name: str, token: str, ttl_ms: int) -> bool:
    res = await get_redis().eval(_EXTEND_LUA, 1, f"xagent:lease:{name}", token, ttl_ms)
    return bool(res)


async def lease_owned(name: str, token: str) -> bool:
    """Fencing: آیا این توکن هنوز مالک Lease است؟ خطای Redis = نامعلوم → False (پیش‌فرض امن برای اثر بیرونی)."""
    try:
        return await get_redis().get(f"xagent:lease:{name}") == token
    except Exception:  # noqa: BLE001
        return False


async def release_lease(name: str, token: str) -> None:
    await get_redis().eval(_RELEASE_LUA, 1, f"xagent:lease:{name}", token)


# ── پرچم‌های سراسری ──
async def is_stop_all() -> bool:
    return bool(await get_redis().get(STOP_ALL_KEY))


async def set_stop_all(on: bool) -> None:
    r = get_redis()
    if on:
        await r.set(STOP_ALL_KEY, "1")
    else:
        await r.delete(STOP_ALL_KEY)


async def request_run_cancel(run_id: uuid.UUID | str) -> None:
    await get_redis().set(f"{RUN_CANCEL_PREFIX}{run_id}", "1", ex=3600)


async def is_run_cancelled(run_id: uuid.UUID | str) -> bool:
    return bool(await get_redis().get(f"{RUN_CANCEL_PREFIX}{run_id}"))


async def clear_run_cancel(run_id: uuid.UUID | str) -> None:
    await get_redis().delete(f"{RUN_CANCEL_PREFIX}{run_id}")


async def is_takeover() -> bool:
    return bool(await get_redis().get(TAKEOVER_KEY))


async def set_takeover(on: bool, by: str = "owner") -> None:
    r = get_redis()
    if on:
        await r.set(TAKEOVER_KEY, by)
    else:
        await r.delete(TAKEOVER_KEY)
