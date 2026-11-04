"""مأموریت جامع — مرحله ۴: تحویل پاسخ نهایی به کانال بیرونی از صندوق خروجی پایدار.

- تحویل دوباره (Crash/اجرای دوباره نهایی‌سازی) پیام تکراری نمی‌فرستد.
- نتیجه نامعلوم ارسال (Timeout پس از ارسال) کورکورانه تکرار نمی‌شود.
- موفقیت کار و موفقیت تحویل اعلان جدا ثبت می‌شوند.
"""

from __future__ import annotations

import uuid

from autonomy_helpers import FakeTelegram
from sqlalchemy import select

from xagent_core.db import session_scope
from xagent_core.models import MissionAction, Run


async def _owner_telegram_run(text: str) -> uuid.UUID:
    async with session_scope() as s:
        run = Run(trigger="telegram", input=text, status="queued",
                  state={"trusted": True, "principal": "owner", "requested_by": "telegram:saved_messages",
                         "reply_to": "me"})
        s.add(run)
        await s.flush()
        return run.id


async def test_final_reply_is_delivered_once_and_recorded(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    fake = FakeTelegram()
    rid = await _owner_telegram_run("سلام، حالت چطوره؟")
    runner = AgentRunner("deliv", telegram=fake)
    await runner.execute(rid)
    assert len(fake.sent) == 1 and fake.sent[0]["chat"] == "me"
    async with session_scope() as s:
        run = await s.get(Run, rid)
        rows = (await s.execute(select(MissionAction).where(MissionAction.dedupe_key == f"final:{rid}:{run.status}"))).scalars().all()
    assert run.state["delivery"]["status"] == "sent" and len(rows) == 1 and rows[0].status == "sent"
    # بازپخش نهایی‌سازی (Crash پس از ارسال و پیش از ثبت نهایی، یا اجرای دوباره) پیام تکراری نمی‌فرستد
    class Rc:
        pass
    rc = Rc()
    rc.run, rc.state = run, dict(run.state)
    await runner._deliver_final(rc, run.status, "همان پاسخ")
    assert len(fake.sent) == 1, "پاسخ نهایی دوباره ارسال شد"
    assert rc.state["delivery"].get("dedupe") is True


async def test_uncertain_final_delivery_is_not_resent_and_task_still_succeeds(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    fake = FakeTelegram()
    fake.fail_after_send = 1  # پیام رفت ولی پاسخ سرور نرسید
    rid = await _owner_telegram_run("سلام دوباره")
    runner = AgentRunner("deliv2", telegram=fake)
    await runner.execute(rid)
    async with session_scope() as s:
        run = await s.get(Run, rid)
    assert run.status == "succeeded", "شکست اعلان، کار انجام‌شده را ناموفق کرد"
    assert run.state["delivery"]["status"] == "unknown"

    class Rc:
        pass
    rc = Rc()
    rc.run, rc.state = run, dict(run.state)
    await runner._deliver_final(rc, run.status, "سلام دوباره")
    assert len(fake.sent) == 1, "ارسال با نتیجه نامعلوم کورکورانه تکرار شد"
    assert rc.state["delivery"]["status"] == "uncertain"


async def test_disconnected_channel_is_reported_not_claimed(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    fake = FakeTelegram(connected=False)
    rid = await _owner_telegram_run("سلام")
    await AgentRunner("deliv3", telegram=fake).execute(rid)
    async with session_scope() as s:
        run = await s.get(Run, rid)
    assert run.status == "succeeded" and run.state["delivery"]["status"] == "not_connected" and fake.sent == []
