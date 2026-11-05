"""Natural-language routine -> approval -> scheduler -> native tool -> receipt -> settlement.

MTProto and the model are controlled doubles; PostgreSQL/Redis and AgentRunner are real.
"""

# ruff: noqa: F811 (imported pytest fixture)
import asyncio
import json
import re
import uuid

import pytest
from autonomy_helpers import FakeTelegram, body_text, called
from sqlalchemy import select
from test_autonomy_acceptance import virtual_clock  # noqa: F401

from xagent_core import bus
from xagent_core.db import session_scope
from xagent_core.models import Approval, Mission, MissionAction, Routine, Run
from xagent_core.settings_store import TelegramSettings, set_setting

pytestmark = pytest.mark.asyncio


async def test_ten_minute_native_reply_happy_path_and_zero_idle_model_calls(
    app_client, configured_model, mock_llm, virtual_clock
):
    from xagent_agent.pipeline import AgentRunner
    from xagent_worker.scheduler import after_routine_run, reconcile_runs, tick_routines

    t0, at = virtual_clock
    fake = FakeTelegram()
    runner = AgentRunner("native-acceptance", telegram=fake)
    marker = "NATIVE-HAPPY-" + uuid.uuid4().hex[:6]
    async with session_scope() as s:
        await set_setting(s, "telegram", TelegramSettings(enabled=True, allowlist=["synthetic"]))
    await bus.set_stop_all(False)

    def handler(body):
        if marker not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if "ساخت-روتین" in body_text(body):
            if not any(n == "routine_create" for n, _ in called(body)):
                return (
                    "routine_create",
                    {
                        "name": marker,
                        "instruction": marker + " پاسخ به پیام مجاز",
                        "authorization": {"reply": True, "reply_to": ["synthetic"]},
                    },
                )
            return "روتین ثبت شد؛ پاسخ‌های آینده در موعد اجرا می‌شوند."
        event = re.search(r'<untrusted_data source="event">\n(.*?)\n</untrusted_data>', body_text(body), re.S)
        events = json.loads(event.group(1)).get("new_messages", []) if event else []
        done = {str(a.get("reply_to")) for n, a in called(body) if n == "telegram_send"}
        for ev in events:
            if str(ev["message_id"]) not in done:
                return (
                    "telegram_send",
                    {"chat": ev["chat"], "reply_to": ev["message_id"], "text": "پاسخ ساختگی"},
                )
        return "بررسی انجام شد؛ نتیجه در رسید ثبت است."

    mock_llm.handlers.insert(0, handler)
    try:
        r = await app_client.post(
            "/api/chat/send",
            json={
                "text": marker + " ساخت-روتین از الان به مدت ده دقیقه هر دقیقه تلگرام را بررسی کن و پاسخ بده"
            },
        )
        rid = uuid.UUID(r.json()["run"]["id"])
        await runner.execute(rid)
        async with session_scope() as s:
            approval = (
                await s.execute(select(Approval).where(Approval.run_id == rid, Approval.status == "pending"))
            ).scalar_one()
            aid = approval.id
        assert (
            await app_client.post(f"/api/approvals/{aid}", json={"decision": "approve"})
        ).status_code == 200
        await runner.execute(rid)
        async with session_scope() as s:
            assert (await s.get(Run, rid)).status == "succeeded"
            routine = (await s.execute(select(Routine).where(Routine.name == marker))).scalar_one()
            routine_id, mission_id = routine.id, routine.mission_id
        idle_calls = []
        for minute in range(10):
            if minute in (2, 6):
                at(minute - 1, 30)
                assert await fake.incoming(811, "synthetic", "پیام ساختگی", 900 + minute)
                assert await fake.incoming(811, "synthetic", "پیام ساختگی", 900 + minute) is None
            at(minute)
            before = len(mock_llm.calls)
            await asyncio.gather(tick_routines(fake), tick_routines(fake))
            assert len(mock_llm.calls) == before
            async with session_scope() as s:
                pending = list(
                    (
                        await s.execute(
                            select(Run).where(Run.routine_id == routine_id, Run.status == "queued")
                        )
                    ).scalars()
                )
            assert len(pending) == 1
            if minute == 6:
                await reconcile_runs(stale_after_seconds=0)
                runner = AgentRunner("restarted-worker", telegram=fake)
            await runner.execute(pending[0].id)
            await after_routine_run(pending[0].id, fake)
            await after_routine_run(pending[0].id, fake)  # duplicate settlement
            async with session_scope() as s:
                rr = await s.get(Run, pending[0].id)
                assert rr.status == "succeeded", rr.result
            if minute not in (2, 6):
                idle_calls.append(len(mock_llm.calls) - before)
        at(10, 1)
        await tick_routines(fake)
        assert idle_calls == [0] * 8
        assert len(fake.sent) == 2
        assert {m["reply_to"] for m in fake.sent} == {902, 906}
        async with session_scope() as s:
            routine = await s.get(Routine, routine_id)
            assert routine.run_count == 10 and routine.failure_count == 0 and routine.status == "completed"
            assert (await s.get(Mission, mission_id)).status == "completed"
            actions = list(
                (
                    await s.execute(
                        select(MissionAction).where(
                            MissionAction.mission_id == mission_id, MissionAction.status == "sent"
                        )
                    )
                ).scalars()
            )
            assert len(actions) == 2
            assert all(a.detail.get("message_id") for a in actions)
    finally:
        mock_llm.handlers.remove(handler)
