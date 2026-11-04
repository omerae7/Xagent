"""Benchmark بخش ۱۹: شش نوع مأموریت با مدل شبیه‌ساز شمارنده (قبل/بعد روی همین فایل).

«توکن» = اندازه پرامپت ارسالی (نویسه/۴)، نه شمارش رسمی ارائه‌دهنده؛ «هزینه» با قیمت ساختگی مدل آزمون (۱$/۲$ در میلیون)
فقط برای مقایسه نسبی است و هیچ ادعای هزینه واقعی از آن نتیجه نمی‌شود. خروجی: BENCH_OUT=<مسیر json> یا pytest -s.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

import pytest
from autonomy_helpers import FakePage, FakeTelegram, body_text, called, tool_names
from sqlalchemy import func, select

from xagent_core.db import session_scope, utcnow
from xagent_core.models import Routine, Run, RunStep

pytestmark = pytest.mark.asyncio
RESULTS: dict[str, dict] = {}


def _script(mark: str, steps):
    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        return steps(body, called(body))
    return handler


def _tok(bodies) -> int:
    return sum(len(json.dumps(b.get("messages", []), ensure_ascii=False)) // 4 for b in bodies)


async def _record(name: str, run_id, bodies) -> dict:
    async with session_scope() as s:
        run = await s.get(Run, run_id)
        tools = (await s.execute(select(func.count()).select_from(RunStep).where(
            RunStep.run_id == run_id, RunStep.kind == "tool"))).scalar_one()
    out = {"llm_calls": len(bodies), "prompt_tokens": _tok(bodies), "tool_calls": int(tools),
           "status": run.status, "mock_cost_usd": round(float(run.cost_usd or 0), 6)}
    RESULTS[name] = out
    if os.environ.get("BENCH_DUMP"):
        with open(f"{os.environ['BENCH_DUMP']}-{name}.json", "w", encoding="utf-8") as f:
            json.dump(bodies, f, ensure_ascii=False, indent=1)
    return out


async def _chat(app_client, runner, mock_llm, text: str, name: str) -> dict:
    before = len(mock_llm.calls)
    r = await app_client.post("/api/chat/send", json={"text": text})
    assert r.status_code == 200, r.text
    rid = uuid.UUID(r.json()["run"]["id"])
    await runner.execute(rid)
    return await _record(name, rid, mock_llm.calls[before:])


async def test_bench_1_simple(app_client, configured_model, runner, mock_llm):
    res = await _chat(app_client, runner, mock_llm, "سلام، امروز چه کمکی از تو برمی‌آید؟ BENCH5-SIMPLE", "simple")
    assert res["status"] == "succeeded"


async def test_bench_2_browser(app_client, configured_model, runner, mock_llm, monkeypatch):
    from xagent_tools.builtin import computer

    page = FakePage(last_page=3)

    async def fake_page(*_, **__):
        return page
    monkeypatch.setattr(computer, "_page", fake_page)

    def steps(body, done):
        names = tool_names(body)
        if "browser_open" not in names:
            return ("tools_load", {"groups": ["browser"]})
        if not any(n == "browser_open" for n, _ in done):
            return ("browser_open", {"url": "https://catalog.example/list"})
        if page.page_no < page.last_page:
            return ("browser_click", {"ref": 1})
        if not any(n == "browser_read" for n, _ in done):
            return ("browser_read", {})
        return "قیمت نهایی محصول هدف ۴۲ دلار است."
    h = _script("BENCH5-BROWSER", steps)
    mock_llm.handlers.insert(0, h)
    try:
        res = await _chat(app_client, runner, mock_llm, "سایت کاتالوگ را باز کن و قیمت محصول هدف را بخوان BENCH5-BROWSER", "browser")
    finally:
        mock_llm.handlers.remove(h)
    assert res["status"] == "succeeded"


async def test_bench_3_multi_step(app_client, configured_model, runner, mock_llm):
    def steps(body, done):
        names = [n for n, _ in done]
        if "task_create" not in names and "task_create" in tool_names(body):
            return ("task_create", {"title": "پیگیری قرارداد بنچمارک", "priority": "high"})
        if "task_list" not in names and "task_list" in tool_names(body):
            return ("task_list", {})
        return "تسک ساخته شد و فهرست تسک‌ها بررسی شد."
    h = _script("BENCH5-MULTI", steps)
    mock_llm.handlers.insert(0, h)
    try:
        res = await _chat(app_client, runner, mock_llm, "یک تسک پیگیری بساز و بعد فهرست تسک‌ها را مرور کن BENCH5-MULTI", "multi_step")
    finally:
        mock_llm.handlers.remove(h)
    assert res["status"] == "succeeded"


async def _routine(app_client, mock_llm, name: str, events: int) -> dict:
    from xagent_agent.pipeline import AgentRunner
    from xagent_worker.scheduler import tick_routines

    mark = f"BENCH5-{name}"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if '<untrusted_data source="event">' in body_text(body):
            return "یک پیام جدید از علی: درباره جلسه فردا."
        if not any(n == "telegram_recent" for n, _ in called(body)) and "telegram_recent" in tool_names(body):
            return ("telegram_recent", {"chat": "ali", "limit": 10})
        return "پیام جدیدی نبود."
    mock_llm.handlers.insert(0, handler)
    try:
        fake = FakeTelegram()
        r = await app_client.post("/api/routines", json={
            "name": f"{name}-{uuid.uuid4().hex[:5]}", "instruction": f"هر دقیقه تلگرام را بررسی کن و پیام تازه را گزارش کن ({mark})",
            "trigger_type": "interval", "interval_seconds": 60, "watch": {"source": "telegram", "chats": ["ali_b5"]}})
        assert r.status_code == 200, r.text
        rid = uuid.UUID(r.json()["id"])
        async with session_scope() as s:
            (await s.get(Routine, rid)).next_run_at = utcnow() - timedelta(seconds=1)
        for i in range(events):
            await fake.incoming(5200 + i, "ali_b5", "سلام، جلسه فردا ساعت چند است؟", 9500 + i, when=utcnow())
        before = len(mock_llm.calls)
        await tick_routines(fake)
        async with session_scope() as s:
            run_id = (await s.execute(select(Run.id).where(Run.routine_id == rid))).scalar_one()
        await AgentRunner("bench5", telegram=fake).execute(run_id)
        return await _record(name, run_id, mock_llm.calls[before:])
    finally:
        mock_llm.handlers.remove(handler)


async def test_bench_4_no_event_routine(app_client, configured_model, mock_llm):
    res = await _routine(app_client, mock_llm, "no_event", 0)
    assert res["llm_calls"] == 0  # پیش‌بررسی قطعی بدون مدل


async def test_bench_5_event_routine(app_client, configured_model, mock_llm):
    res = await _routine(app_client, mock_llm, "event", 3)
    assert res["llm_calls"] >= 1


async def test_bench_6_tool_failure(app_client, configured_model, runner, mock_llm, monkeypatch):
    from xagent_tools import net
    from xagent_tools.builtin import computer

    page = FakePage(last_page=1)

    async def fake_page(*_, **__):
        return page
    monkeypatch.setattr(computer, "_page", fake_page)

    async def broken(method, url, **kw):
        return net.SafeResponse(503, url, __import__("httpx").Headers({"content-type": "text/plain"}), b"Service Unavailable")
    monkeypatch.setattr(net, "safe_request", broken)

    def steps(body, done):
        names = tool_names(body)
        if not any(n == "http_request" for n, _ in done) and "http_request" in names:
            return ("http_request", {"method": "GET", "url": "https://api.catalog.example/items"})
        if "browser_open" not in names and not any(n == "tools_load" for n, _ in done):
            return ("tools_load", {"groups": ["browser"]})  # بازیابی به عهده مدل (اگر سیستم خودش جایگزین نکند)
        if "browser_open" in names and not any(n == "browser_open" for n, _ in done):
            return ("browser_open", {"url": "https://api.catalog.example/items"})
        return "از طریق مرورگر: قیمت نهایی ۴۲ دلار است."
    h = _script("BENCH5-FAIL", steps)
    mock_llm.handlers.insert(0, h)
    try:
        await _chat(app_client, runner, mock_llm, "از API کاتالوگ قیمت را بگیر http و گزارش بده BENCH5-FAIL", "tool_failure")
    finally:
        mock_llm.handlers.remove(h)


async def test_bench_zz_report():
    print("\nBENCH5", json.dumps(RESULTS, ensure_ascii=False))
    out = os.environ.get("BENCH_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, ensure_ascii=False, indent=2)
