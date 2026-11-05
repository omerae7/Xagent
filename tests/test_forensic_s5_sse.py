"""ممیزی جرم‌شناختی — مرحله ۵، F16: نظارت پمپ SSE؛ قطع Redis جریان «سالمِ بی‌رویداد» نمی‌سازد.

API واقعی Uvicorn و Redis محلی واقعی؛ اتصال Pub/Sub با CLIENT KILL قطع می‌شود (شبیه قطعی Redis).
"""

from __future__ import annotations

import asyncio

import httpx
from test_master_worker import _login, live_api

from xagent_core import bus


async def _read_until(resp, it, needle: str, timeout: float) -> str:
    buf = ""

    async def go():
        nonlocal buf
        async for chunk in it:
            buf += chunk
            if needle in buf:
                return
    try:
        await asyncio.wait_for(go(), timeout=timeout)
    except TimeoutError:
        pass
    return buf


async def test_F16_pubsub_disconnect_is_detected_resynced_and_events_resume(app_client, monkeypatch):
    from xagent_api.routers import live

    monkeypatch.setattr(live, "SSE_REAUTH_SECONDS", 3600)
    async with live_api() as base, httpx.AsyncClient(base_url=base, timeout=30) as client:
        await _login(client)
        async with client.stream("GET", "/api/events/stream") as resp:
            it = resp.aiter_text()
            assert "reason" in await _read_until(resp, it, "connected", 5)
            await asyncio.sleep(0.3)
            await bus.publish("notification", {"title": "F16-before"})
            assert "F16-before" in await _read_until(resp, it, "F16-before", 5)
            await bus.get_redis().execute_command("CLIENT", "KILL", "TYPE", "pubsub")  # قطع اتصال Pub/Sub
            got = await _read_until(resp, it, "recovered", 12)
            assert "recovered" in got, f"قطع پمپ تشخیص/همگام‌سازی دوباره نشد؛ جریان فقط ping فرستاد: {got[-300:]!r}"
            await bus.publish("notification", {"title": "F16-after"})
            assert "F16-after" in await _read_until(resp, it, "F16-after", 6), "پس از بازیابی رویدادها دوباره نرسیدند"
