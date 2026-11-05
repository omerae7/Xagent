"""ممیزی جرم‌شناختی — ریسک‌های معماری اضافی که اصلاح محدود و امن داشتند.

۱) بازپخش اثر بیرونی عمومی پس از Crash؛ ۲) اسرار پیش‌فرض ناامن در production.
"""

from __future__ import annotations

import pytest
from autonomy_helpers import body_text, called, tool_names

from xagent_core.db import session_scope
from xagent_core.models import Run
from xagent_tools.base import Tool, ToolResult, obj, s

EFFECTS: list[str] = []
CRASH = {"armed": True}


class Crash(BaseException):
    """مرگ ناگهانی فرایند Worker پس از انجام اثر و پیش از ثبت نتیجه (شبیه‌سازی)."""


async def _fx_pay_vendor(ctx, a):
    EFFECTS.append(a.get("n", ""))
    if CRASH["armed"]:
        CRASH["armed"] = False
        raise Crash()
    return ToolResult(True, "ثبت شد", {"n": a.get("n")}, verified=True)


FX = Tool("fx_record", "ثبت اثر بیرونی آزمایشی", obj({"n": s("شماره")}), _fx_pay_vendor, "tasks",
          category="routine_change", label="ثبت اثر")


async def test_risk_effect_is_not_replayed_after_crash(app_client, configured_model, mock_llm, monkeypatch):
    from xagent_agent import pipeline

    orig = pipeline.select_tools

    async def with_fx(session, groups, query):
        return [*(await orig(session, groups, query)), FX]
    monkeypatch.setattr(pipeline, "select_tools", with_fx)
    EFFECTS.clear()
    CRASH["armed"] = True
    mark = "RISK-REPLAY"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if "fx_record" not in tool_names(body):
            return None
        if not any(n == "fx_record" for n, _ in called(body)):
            return ("fx_record", {"n": "once"})
        return "گزارش."
    async with session_scope() as ss:
        run = Run(trigger="chat", input=f"اثر را ثبت کن ({mark})", status="queued", state={"trusted": True})
        ss.add(run)
        await ss.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        with pytest.raises(Crash):
            await pipeline.AgentRunner("risk").execute(rid)
        async with session_scope() as ss:  # بازیابی: Run دوباره در صف قرار می‌گیرد
            r = await ss.get(Run, rid)
            assert r.state.get("effect_intents"), "قصد اثر پیش از اجرای آن پایدار نشده بود"
            r.status = "queued"
        await pipeline.AgentRunner("risk").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    assert EFFECTS == ["once"], f"اثر بیرونی پس از Crash دوباره اجرا شد: {EFFECTS}"
    async with session_scope() as ss:
        r = await ss.get(Run, rid)
    assert r.status != "succeeded", "نتیجه نامعلوم اثر به‌عنوان موفقیت ثبت شد"
    assert any("دوباره اجرا نشد" in (e.get("summary") or "") for e in r.state["evidence"])


def test_risk_production_refuses_known_default_secrets():
    from xagent_core.config import Settings, production_secret_problems

    weak = Settings(env="production", secret_key="change-me-in-production-please-32+chars",
                    desktop_token="change-me-desktop-token")
    blocking, _ = production_secret_problems(weak)
    assert len(blocking) == 2 and all("پیش‌فرض" in b for b in blocking)
    assert "change-me" not in " ".join(blocking), "مقدار سری در پیام خطا آمد"
    strong = Settings(env="production", secret_key="a" * 64, desktop_token="b" * 32)
    assert production_secret_problems(strong) == ([], [])
    short = Settings(env="production", secret_key="short-but-custom", desktop_token="b" * 32)
    blocking, warnings = production_secret_problems(short)
    assert blocking == [] and warnings, "کلید کوتاه سفارشی باید فقط هشدار بدهد (بدون ازکارافتادن نصب موجود)"
    assert production_secret_problems(Settings(env="test")) == ([], [])


async def test_risk_control_command_intake_is_bounded(app_client, monkeypatch):
    from xagent_core import bus

    r = bus.get_redis()
    await r.delete(bus.CONTROL_STREAM)
    monkeypatch.setattr(bus, "CONTROL_MAX_BACKLOG", 3)
    codes = [(await app_client.post("/api/tools/install", json={"source": "https://github.com/example/risk"})).status_code
             for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], f"ورودی فرمان کنترلی نامحدود است: {codes}"
    assert await r.xlen(bus.CONTROL_STREAM) == 3
    await r.delete(bus.CONTROL_STREAM)


async def test_risk_concurrent_bootstrap_is_serialized(app_client):
    import asyncio

    from sqlalchemy import delete, func, select

    from xagent_api.bootstrap import bootstrap
    from xagent_core.models import HarnessRule

    async with session_scope() as ss:
        await ss.execute(delete(HarnessRule).where(HarnessRule.category == "memory_rule"))
    results = await asyncio.gather(*[bootstrap() for _ in range(4)], return_exceptions=True)
    assert not [r for r in results if isinstance(r, BaseException)], f"راه‌اندازی هم‌زمان خطا داد: {results}"
    async with session_scope() as ss:
        n = (await ss.execute(select(func.count()).select_from(HarnessRule)
                              .where(HarnessRule.category == "memory_rule"))).scalar_one()
    assert n == 1


async def test_risk_long_lived_vnc_session_closes_after_token_revocation(app_client, monkeypatch):
    import asyncio

    import websockets
    from sqlalchemy import select
    from test_master_worker import _login, live_api

    from xagent_api.routers import integrations
    from xagent_core.config import get_settings
    from xagent_core.models import User

    async def upstream(conn):
        async for _ in conn:
            pass
    server = await websockets.serve(upstream, "127.0.0.1", 0, subprotocols=["binary"])
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(get_settings(), "desktop_vnc_ws_view", f"ws://127.0.0.1:{port}/", raising=False)
    monkeypatch.setattr(integrations, "VNC_REAUTH_SECONDS", 1)
    import httpx
    try:
        async with live_api() as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
            await _login(client)
            tok = next(c.value for c in client.cookies.jar if c.name == "xa_token")
            ws_url = base.replace("http", "ws") + "/api/desktop/vnc?mode=view"
            async with websockets.connect(ws_url, additional_headers={"Cookie": f"xa_token={tok}"},
                                          subprotocols=["binary"]) as ws:
                async with session_scope() as ss:  # ابطال همه نشست‌ها در میانه جلسه VNC
                    u = (await ss.execute(select(User).where(User.username == "admin"))).scalar_one()
                    u.token_version += 1
                closed = False
                try:
                    await asyncio.wait_for(ws.recv(), timeout=8)
                except websockets.ConnectionClosed:
                    closed = True
                except TimeoutError:
                    closed = False
                assert closed, "جلسه VNC پس از ابطال توکن باز ماند"
    finally:
        server.close()
        r = await app_client.post("/api/auth/login", json={"username": "admin", "password": "Test-Password-123"})
        app_client.cookies.set("xa_token", r.json()["token"])
