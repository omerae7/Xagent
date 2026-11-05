"""ممیزی جرم‌شناختی — مرحله ۲: تازگی مجوز در لحظه اثر (F03) و لغو/توقف اضطراری پیش از هر اثر (F06).

ابزارهای ساختگی fx_* فقط فراخوانی خود را ثبت می‌کنند (هیچ اثر بیرونی واقعی). تغییرهای هم‌زمان (لغو تأیید،
توقف سراسری، لغو اجرا، غیرفعال‌سازی کارمند، تغییر سیاست) در نشست مستقل پایگاه داده/Redis انجام می‌شوند.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from autonomy_helpers import FakeTelegram, body_text, called, tool_names
from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Approval, Employee, HarnessRule, Mission, Routine, Run
from xagent_tools.base import Tool, ToolResult, obj, s

EFFECTS: list[str] = []
HOOK: dict[str, object] = {}


async def _fx_effect(ctx, a):
    EFFECTS.append(f"effect:{a.get('n', '')}")
    return ToolResult(True, f"اثر {a.get('n', '')} ثبت شد", {"n": a.get("n")}, verified=True)


async def _fx_send(ctx, a):
    EFFECTS.append(f"send:{a.get('n', '')}")
    return ToolResult(True, "ارسال شد", {"n": a.get("n")}, verified=True)


async def _fx_hook(ctx, a):
    fn = HOOK.get("fn")
    if fn is not None:
        await fn(ctx)
    return ToolResult(True, "بررسی شد", {"hook": True})


FX = [
    Tool("fx_effect", "اثر آزمایشی", obj({"n": s("شماره")}), _fx_effect, "tasks", category="routine_change",
         label="اثر آزمایشی"),
    Tool("fx_send", "ارسال آزمایشی", obj({"n": s("شماره")}), _fx_send, "tasks", category="external_message",
         label="ارسال آزمایشی"),
    Tool("fx_hook", "بررسی آزمایشی", obj({}), _fx_hook, "tasks", label="بررسی آزمایشی"),
]


@pytest.fixture(autouse=True)
def fx_tools(monkeypatch):
    from xagent_agent import pipeline

    orig = pipeline.select_tools

    async def with_fx(session, groups, query):
        return [*(await orig(session, groups, query)), *FX]
    monkeypatch.setattr(pipeline, "select_tools", with_fx)
    EFFECTS.clear()
    HOOK.clear()
    yield
    HOOK.clear()


def batch_once(mark: str, batch: list[tuple[str, dict]]):
    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if "fx_effect" not in tool_names(body):
            return None
        if not any(n.startswith("fx_") for n, _ in called(body)):
            return batch
        return "گزارش کار."
    return handler


async def run_with(mock_llm, mark: str, handler, *, telegram=None, **run_kw) -> Run:
    from xagent_agent.pipeline import AgentRunner

    async with session_scope() as ss:
        run = Run(trigger=run_kw.pop("trigger", "chat"), input=f"کار آزمایشی ({mark})", status="queued",
                  state={"trusted": True}, **run_kw)
        ss.add(run)
        await ss.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("s2", telegram=telegram).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as ss:
        return await ss.get(Run, rid)


# ───────────────────────── F06: توقف/لغو پیش از هر اثر ─────────────────────────
async def test_F06_emergency_stop_blocks_later_call_in_same_batch(app_client, configured_model, mock_llm):
    async def stop_all(ctx):
        await bus.set_stop_all(True)
    HOOK["fn"] = stop_all
    try:
        run = await run_with(mock_llm, "F06-STOP", batch_once("F06-STOP", [("fx_hook", {}), ("fx_effect", {"n": "1"})]))
    finally:
        await bus.set_stop_all(False)
    assert "effect:1" not in EFFECTS, "پس از توقف اضطراری، فراخوانی بعدی همان Batch اجرا شد"
    assert run.status != "succeeded"


async def test_F06_run_cancel_blocks_later_call_in_same_batch(app_client, configured_model, mock_llm):
    async def cancel(ctx):
        r = await app_client.post(f"/api/runs/{ctx.run_id}/cancel")
        assert r.status_code == 200
    HOOK["fn"] = cancel
    run = await run_with(mock_llm, "F06-CANCEL", batch_once("F06-CANCEL", [("fx_hook", {}), ("fx_effect", {"n": "2"})]))
    assert "effect:2" not in EFFECTS, "پس از لغو اجرا، اثر بعدی اجرا شد"
    assert run.status in ("stopped", "cancelled"), run.status


async def test_F06_queued_run_of_cancelled_routine_does_not_execute(app_client, configured_model, mock_llm):
    async with session_scope() as ss:
        rt = Routine(name=f"روتین لغوشده {uuid.uuid4().hex[:6]}", instruction="x", trigger_type="interval",
                     interval_seconds=600, enabled=False, status="cancelled")
        ss.add(rt)
        await ss.flush()
        rtid = rt.id
    before = len(mock_llm.calls)
    run = await run_with(mock_llm, "F06-ROUTINE", batch_once("F06-ROUTINE", [("fx_effect", {"n": "3"})]),
                         trigger="routine", routine_id=rtid)
    assert "effect:3" not in EFFECTS and run.status in ("cancelled", "stopped"), run.status
    assert len(mock_llm.calls) == before, "اجرای روتین لغوشده به مدل رسید"


async def test_F06_child_of_cancelled_parent_never_starts(app_client, configured_model, mock_llm):
    async with session_scope() as ss:
        parent = Run(trigger="chat", input="والد", status="cancelled", state={"trusted": True},
                     finished_at=utcnow() - timedelta(hours=2))
        ss.add(parent)
        await ss.flush()
        pid = parent.id
    await bus.clear_run_cancel(pid)  # پرچم Redis منقضی شده (TTL)؛ فقط وضعیت پایدار پایگاه داده باقی است
    run = await run_with(mock_llm, "F06-CHILD", batch_once("F06-CHILD", [("fx_effect", {"n": "4"})]),
                         trigger="delegate", parent_run_id=pid)
    assert "effect:4" not in EFFECTS and run.status in ("cancelled", "stopped"), run.status


async def test_F06_cancel_propagates_to_queued_delegated_children(app_client):
    async with session_scope() as ss:
        parent = Run(trigger="chat", input="والد", status="running", state={"trusted": True})
        ss.add(parent)
        await ss.flush()
        child = Run(trigger="delegate", input="فرزند", status="queued", state={"trusted": True}, parent_run_id=parent.id)
        ss.add(child)
        await ss.flush()
        grand = Run(trigger="delegate", input="نوه", status="queued", state={"trusted": True}, parent_run_id=child.id)
        ss.add(grand)
        await ss.flush()
        pid, cid, gid = parent.id, child.id, grand.id
    r = await app_client.post(f"/api/runs/{pid}/cancel")
    assert r.status_code == 200
    async with session_scope() as ss:
        for x in (cid, gid):
            assert (await ss.get(Run, x)).status == "cancelled", "لغو والد به فرزند واگذارشده منتقل نشد"
            assert await bus.is_run_cancelled(x)


# ───────────────────────── F03: تازگی مجوز در لحظه اثر ─────────────────────────
async def test_F03_approval_revoked_in_independent_session_blocks_effect(app_client, configured_model, mock_llm,
                                                                         monkeypatch):
    from xagent_agent import pipeline

    mark = "F03-REVOKE"
    run = await run_with(mock_llm, mark, batch_once(mark, [("fx_send", {"n": "5"})]))
    assert run.status == "waiting_approval"
    async with session_scope() as ss:
        ap = (await ss.execute(select(Approval).where(Approval.run_id == run.id))).scalar_one()
    r = await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "approve"})
    assert r.status_code == 200
    orig = pipeline.AgentRunner._harness_decide

    async def racing(self, rc, cid, tool, category, args, rules, **kw):
        d = await orig(self, rc, cid, tool, category, args, rules, **kw)
        if d == "allow" and tool.name == "fx_send":  # همزمان: کارفرما در نشست دیگر تأیید را باطل می‌کند
            r2 = await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "revoke"})
            assert r2.status_code == 200, r2.text
        return d
    monkeypatch.setattr(pipeline.AgentRunner, "_harness_decide", racing)
    mock_llm.handlers.insert(0, batch_once(mark, [("fx_send", {"n": "5"})]))
    try:
        await pipeline.AgentRunner("s2").execute(run.id)
    finally:
        mock_llm.handlers.pop(0)
    assert "send:5" not in EFFECTS, "تأیید باطل‌شده در نشست مستقل همچنان اثر را مجاز کرد"


async def test_F03_policy_tightened_mid_run_blocks_next_effect(app_client, configured_model, mock_llm):
    async def forbid(ctx):
        async with session_scope() as ss:
            row = (await ss.execute(select(HarnessRule).where(HarnessRule.category == "routine_change"))).scalar_one_or_none()
            if row is None:
                ss.add(HarnessRule(category="routine_change", mode="forbidden"))
            else:
                row.mode = "forbidden"
    HOOK["fn"] = forbid
    try:
        await run_with(mock_llm, "F03-POLICY", batch_once("F03-POLICY", [("fx_hook", {}), ("fx_effect", {"n": "6"})]))
    finally:
        async with session_scope() as ss:
            row = (await ss.execute(select(HarnessRule).where(HarnessRule.category == "routine_change"))).scalar_one_or_none()
            if row is not None:
                row.mode = "auto"
    assert "effect:6" not in EFFECTS, "سیاست «ممنوع» تازه پیش از اثر بعدی اعمال نشد"


async def test_F03_employee_disabled_mid_run_blocks_next_effect(app_client, configured_model, mock_llm):
    async with session_scope() as ss:
        emp = Employee(name=f"کارمند-{uuid.uuid4().hex[:6]}", title="آزمون", tool_groups=[], enabled=True)
        ss.add(emp)
        await ss.flush()
        eid = emp.id

    async def disable(ctx):
        async with session_scope() as ss:
            (await ss.get(Employee, eid)).enabled = False
    HOOK["fn"] = disable
    await run_with(mock_llm, "F03-EMP", batch_once("F03-EMP", [("fx_hook", {}), ("fx_effect", {"n": "7"})]),
                   employee_id=eid)
    assert "effect:7" not in EFFECTS, "کارمند غیرفعال‌شده اثر بعدی را اجرا کرد"


async def test_F03_mission_revoked_in_independent_session_blocks_next_send(app_client, configured_model, mock_llm):
    from test_master_authz import SCOPE

    async with session_scope() as ss:
        m = Mission(title="F03 مأموریت", owner="admin", status="active", scope=dict(SCOPE), actions_count=0,
                    expires_at=utcnow() + timedelta(hours=1))
        ss.add(m)
        await ss.flush()
        mid = m.id
    fake = FakeTelegram()
    orig_send = fake.send

    async def send_then_revoke(chat, text, **kw):
        out = await orig_send(chat, text, **kw)
        r = await app_client.post(f"/api/missions/{mid}/revoke")
        assert r.status_code == 200, r.text
        return out
    fake.send = send_then_revoke
    mark = "F03-MISSION"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if not any(n == "telegram_send" for n, _ in called(body)):
            return [("telegram_send", {"chat": "ali", "text": "اول"}), ("telegram_send", {"chat": "ali", "text": "دوم"})]
        return "گزارش."
    from xagent_agent.pipeline import AgentRunner

    async with session_scope() as ss:
        run = Run(trigger="routine", input=f"کار مأموریت ({mark})", status="queued",
                  state={"trusted": True, "principal": "mission", "mission_id": str(mid)})
        ss.add(run)
        await ss.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("s2m", telegram=fake).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    texts = [x["text"] for x in fake.sent]
    assert "اول" in texts and "دوم" not in texts, f"پس از لغو مأموریت پیام دوم ارسال شد: {texts}"


async def test_F03_telegram_approval_channel_enforces_pending_ttl_and_unique_id(app_client):
    from xagent_worker.telegram import handle_owner_command

    async with session_scope() as ss:
        run = Run(trigger="chat", input="x", status="waiting_approval", state={})
        ss.add(run)
        await ss.flush()
        old = Approval(run_id=run.id, tool_call_id="c-old", category="external_message", tool_name="fx_send",
                       args={}, created_at=utcnow() - timedelta(days=10))
        ss.add(old)
        await ss.flush()
        oid = old.id
    fake = FakeTelegram()
    await handle_owner_command(fake, {"text": f"تایید {str(oid)[:8]}"})
    async with session_scope() as ss:
        assert (await ss.get(Approval, oid)).status != "approved", "تأیید تلگرامی TTL کارت قدیمی را نادیده گرفت"
