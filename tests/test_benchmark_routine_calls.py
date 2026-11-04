"""اندازه‌گیری واقعی فراخوانی مدل در هر مسیر اجرای روتین (Benchmark بخش ۱۲).

با مدل شبیه‌ساز شمارنده؛ «توکن» اینجا اندازه پرامپت ارسالی (نویسه/۴) است، نه شمارش رسمی ارائه‌دهنده.
اعداد با `pytest -s tests/test_benchmark_routine_calls.py` چاپ می‌شوند و در گزارش فنی ثبت شده‌اند.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from autonomy_helpers import FakeTelegram, body_text, called
from sqlalchemy import select

from xagent_core.db import session_scope, utcnow
from xagent_core.models import Routine, Run

MARK = "BENCH-ROUTINE-12"
INSTRUCTION = f"هر دقیقه تلگرام را بررسی کن و اگر پیام جدیدی بود خلاصه‌اش را گزارش کن ({MARK})"


def handler(body):
    if MARK not in body_text(body) or not body.get("tools") or body.get("response_format"):
        return None
    txt = body_text(body)
    if '<untrusted_data source="event">' in txt:
        return "یک پیام جدید از علی دریافت شد: درباره جلسه فردا."
    # مسیر کامل بدون پیش‌بررسی: مدل خودش باید تلگرام را بخواند
    if not any(n == "telegram_recent" for n, _ in called(body)) and "telegram_recent" in {t["function"]["name"] for t in body["tools"]}:
        return ("telegram_recent", {"chat": "ali", "limit": 10})
    return "پیام جدیدی نبود."


async def _measure(app_client, mock_llm, watch: dict | None, events: int) -> dict:
    from xagent_agent.pipeline import AgentRunner
    from xagent_worker.scheduler import tick_routines

    fake = FakeTelegram()
    r = await app_client.post("/api/routines", json={
        "name": f"bench-{uuid.uuid4().hex[:5]}", "instruction": INSTRUCTION, "trigger_type": "interval",
        "interval_seconds": 60, **({"watch": watch} if watch else {})})
    rid = uuid.UUID(r.json()["id"])
    async with session_scope() as s:
        (await s.get(Routine, rid)).next_run_at = utcnow() - timedelta(seconds=1)
    for i in range(events):
        await fake.incoming(4100 + i, "ali_bench", "سلام، جلسه فردا ساعت چند است؟", 9100 + i,
                            when=utcnow())
    before = len(mock_llm.calls)
    await tick_routines(fake)
    async with session_scope() as s:
        run_id = (await s.execute(select(Run.id).where(Run.routine_id == rid))).scalar_one()
    await AgentRunner("bench", telegram=fake).execute(run_id)
    bodies = mock_llm.calls[before:]
    async with session_scope() as s:
        run = await s.get(Run, run_id)
    return {"calls": len(bodies), "prompt_tokens": sum(len(json.dumps(b.get("messages", []), ensure_ascii=False)) // 4 for b in bodies),
            "purposes": [("json" if b.get("response_format") else "exec") for b in bodies], "status": run.status,
            "outcome": run.outcome}


async def test_benchmark_llm_calls_per_routine_path(app_client, configured_model, mock_llm):
    mock_llm.handlers.insert(0, handler)
    try:
        full = await _measure(app_client, mock_llm, None, 0)
        idle = await _measure(app_client, mock_llm, {"source": "telegram", "chats": ["ali_bench"]}, 0)
        one = await _measure(app_client, mock_llm, {"source": "telegram", "chats": ["ali_bench"]}, 1)
    finally:
        mock_llm.handlers.remove(handler)
    print("\nBENCHMARK routine-run LLM calls:",
          json.dumps({"full_path_no_watch": full, "watch_no_event": idle, "watch_one_event": one}, ensure_ascii=False))
    # مسیر کامل قبلی: هدف‌سنجی + برنامه‌ریزی + اجرا (با ابزار) + ارزیابی
    assert full["calls"] >= 4 and full["purposes"].count("json") >= 3
    # پیش‌بررسی بدون رویداد: صفر فراخوانی
    assert idle["calls"] == 0 and idle["status"] == "succeeded" and idle["outcome"] == "no_change"
    # رویداد جدید: فقط مسیر سبک (بدون هدف/برنامه/ارزیابی LLM)
    assert one["calls"] == 1 and one["purposes"] == ["exec"] and one["outcome"] == "changed"
