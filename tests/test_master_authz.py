"""مأموریت جامع — مرحله ۱: مجوز در لحظه اجرا (سناریوهای C1–C4 و مرز ابزار کارمند).

همه با PostgreSQL/Redis واقعی و مدل/تلگرام شبیه‌ساز؛ هیچ پیام واقعی ارسال نمی‌شود.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from autonomy_helpers import FakeTelegram, body_text, called, tool_names
from sqlalchemy import select

from xagent_core.db import session_scope, utcnow
from xagent_core.models import Approval, Employee, Mission, Run, RunStep

SCOPE = {"tools": ["telegram_recent", "telegram_send", "notify_owner"],
         "telegram": {"read": True, "read_chats": ["ali"], "reply": True, "reply_to": ["ali"],
                      "reply_only_to_senders": True, "max_replies_per_chat_hour": 6, "allow_files": False,
                      "send_standalone": True},
         "out_of_scope": "ask"}


async def _mission(**over) -> Mission:
    async with session_scope() as s:
        m = Mission(title="آزمون مجوز", owner="admin", status="active", scope={**SCOPE, **over}, actions_count=0,
                    expires_at=utcnow() + timedelta(hours=1))
        s.add(m)
        await s.flush()
        return m


async def _mission_run(m: Mission, mark: str) -> uuid.UUID:
    async with session_scope() as s:
        run = Run(trigger="routine", input=f"کار مأموریت ({mark})", status="queued",
                  state={"trusted": True, "principal": "mission", "mission_id": str(m.id)})
        s.add(run)
        await s.flush()
        return run.id


def _once(mark: str, call: tuple[str, dict]):
    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if not any(n == call[0] for n, _ in called(body)):
            return call
        return "گزارش: انجام نشد یا منتظر اجازه است."
    return handler


async def _execute(runner_name: str, rid, fake, mock_llm, handler):
    from xagent_agent.pipeline import AgentRunner

    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner(runner_name, telegram=fake).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        return await s.get(Run, rid)


# ── C1: «ask» مأموریت برای ابزار بدون دسته Harness نباید به اجرا برسد ──
async def test_C1_mission_ask_on_category_less_read_tool_waits_for_approval(app_client, configured_model, mock_llm):
    m = await _mission()
    fake = FakeTelegram()
    fake.history["bob"] = [{"id": 1, "date": "", "sender": "bob", "text": "C1-SECRET"}]
    mark = "C1-ASK-READ"
    rid = await _mission_run(m, mark)
    before = len(mock_llm.calls)
    run = await _execute("c1", rid, fake, mock_llm, _once(mark, ("telegram_recent", {"chat": "bob", "limit": 5})))
    assert "C1-SECRET" not in json.dumps(mock_llm.calls[before:], ensure_ascii=False), "خواندن خارج از محدوده اجرا شد"
    assert run.status == "waiting_approval", run.status
    async with session_scope() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalar_one()
    assert ap.tool_name == "telegram_recent" and ap.status == "pending"


# ── C1b: قاعده «خودکار» Harness نمی‌تواند «ask» مأموریت را دور بزند ──
async def test_C1b_mission_ask_is_not_bypassed_by_auto_harness(app_client, configured_model, mock_llm):
    r = await app_client.put("/api/settings/harness/external_message", json={"mode": "auto"})
    assert r.status_code == 200, r.text
    try:
        m = await _mission()
        fake = FakeTelegram()
        mark = "C1B-ASK-SEND"
        rid = await _mission_run(m, mark)
        run = await _execute("c1b", rid, fake, mock_llm, _once(mark, ("telegram_send", {"chat": "eve", "text": "سلام"})))
        assert fake.sent == [], "ارسال خارج از محدوده بدون اجازه انجام شد"
        assert run.status == "waiting_approval", run.status
    finally:
        await app_client.put("/api/settings/harness/external_message", json={"mode": "ask"})


# ── C2: تأیید برای پارامترهای دیگر، عملیات تغییر‌یافته را مجاز نمی‌کند ──
async def test_C2_approval_is_bound_to_exact_operation(app_client, configured_model, mock_llm):
    m = await _mission()
    fake = FakeTelegram()
    mark = "C2-BINDING"
    rid = await _mission_run(m, mark)
    handler = _once(mark, ("telegram_send", {"chat": "eve", "text": "متن تأییدشده"}))
    run = await _execute("c2", rid, fake, mock_llm, handler)
    assert run.status == "waiting_approval"
    async with session_scope() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalar_one()
    r = await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    # عملیات پیش از اجرا تغییر می‌کند (متن دیگر) — تأیید قبلی نباید آن را پوشش دهد
    async with session_scope() as s:
        row = await s.get(Run, rid)
        st = dict(row.state)
        pend = [dict(c) for c in st["pending_calls"]]
        pend[0]["arguments"] = {**pend[0]["arguments"], "text": "متن تغییرکرده و تأییدنشده"}
        st["pending_calls"] = pend
        row.state = st
    run = await _execute("c2", rid, fake, mock_llm, handler)
    assert fake.sent == [], f"عملیات تغییرکرده با تأیید قدیمی اجرا شد: {fake.sent}"
    assert run.status != "succeeded"


# ── C2b: تأیید درست همان عملیات را اجرا می‌کند (سازگاری) ──
async def test_C2b_matching_approval_executes_once(app_client, configured_model, mock_llm):
    m = await _mission()
    fake = FakeTelegram()
    mark = "C2B-MATCH"
    rid = await _mission_run(m, mark)
    handler = _once(mark, ("telegram_send", {"chat": "eve", "text": "سلام از آزمون"}))
    await _execute("c2b", rid, fake, mock_llm, handler)
    async with session_scope() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalar_one()
    assert (await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "approve"})).status_code == 200
    await _execute("c2b", rid, fake, mock_llm, handler)
    assert len(fake.sent) == 1 and fake.sent[0]["text"] == "سلام از آزمون"
    # فهرست تأییدها جزئیات اتصال داخلی را نمایش نمی‌دهد
    items = (await app_client.get("/api/approvals?status=all")).json()["items"]
    assert all("__binding" not in (i.get("args") or {}) for i in items)


# ── C3: تأیید کهنه رد می‌شود ──
async def test_C3_stale_approval_is_rejected(app_client, configured_model, mock_llm):
    m = await _mission()
    fake = FakeTelegram()
    mark = "C3-STALE"
    rid = await _mission_run(m, mark)
    handler = _once(mark, ("telegram_send", {"chat": "eve", "text": "پیام کهنه"}))
    await _execute("c3", rid, fake, mock_llm, handler)
    async with session_scope() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalar_one()
        ap.status, ap.decided_at, ap.decided_by = "approved", utcnow() - timedelta(days=3), "admin"
        (await s.get(Run, rid)).status = "queued"
    run = await _execute("c3", rid, fake, mock_llm, handler)
    assert fake.sent == [], "تأیید منقضی‌شده عملیات را مجاز کرد"
    assert run.status != "succeeded"
    async with session_scope() as s:
        assert (await s.get(Approval, ap.id)).status == "expired"


# ── C4: مأموریت لغوشده پس از تأیید، در لحظه اجرا رد می‌شود ──
async def test_C4_revoked_mission_is_rechecked_at_use_time(app_client, configured_model, mock_llm):
    m = await _mission()
    fake = FakeTelegram()
    mark = "C4-REVOKED"
    rid = await _mission_run(m, mark)
    handler = _once(mark, ("telegram_send", {"chat": "eve", "text": "پس از لغو"}))
    await _execute("c4", rid, fake, mock_llm, handler)
    async with session_scope() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalar_one()
    assert (await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "approve"})).status_code == 200
    async with session_scope() as s:
        (await s.get(Mission, m.id)).status = "revoked"
    run = await _execute("c4", rid, fake, mock_llm, handler)
    assert fake.sent == [], "مأموریت لغوشده با تأیید قبلی اجرا شد"
    assert run.status != "succeeded"


# ── مرز ابزار کارمند: tools_load اختیار را فراتر از tool_groups نمی‌برد ──
async def test_employee_tool_groups_are_an_execution_boundary(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    async with session_scope() as s:
        emp = Employee(name=f"محدود-{uuid.uuid4().hex[:4]}", title="پژوهشگر", tool_groups=["web"], enabled=True)
        s.add(emp)
        await s.flush()
        run = Run(trigger="api", input="فقط جست‌وجو کن (EMP-BOUNDARY)", status="queued", employee_id=emp.id,
                  state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    offered: list[set[str]] = []

    def handler(body):
        if "EMP-BOUNDARY" not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        offered.append(tool_names(body))
        done = [n for n, _ in called(body)]
        if "tools_load" not in done:
            return ("tools_load", {"groups": ["files", "system"]})
        if "file_write" not in done:
            return ("file_write", {"path": "emp/escape.txt", "content": "x"})
        return "نتوانستم."
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("emp").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    assert all("file_write" not in o and "shell_exec" not in o for o in offered), offered
    async with session_scope() as s:
        steps = (await s.execute(select(RunStep).where(RunStep.run_id == rid, RunStep.kind == "tool",
                                                       RunStep.tool_name == "file_write",
                                                       RunStep.status == "ok"))).scalars().all()
    assert not steps, "کارمند ابزار خارج از اختیار خود را اجرا کرد"
