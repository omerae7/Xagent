"""ممیزی جرم‌شناختی — مرحله ۳، F07: اعتبارسنجی هدف واقعی هر معیار (نه شمار ابزارهای موفق).

مدل و تلگرام شبیه‌سازند؛ هیچ پیام واقعی ارسال نمی‌شود.
"""

from __future__ import annotations

from autonomy_helpers import FakeTelegram, body_text, called, tool_names

from xagent_core.db import session_scope
from xagent_core.models import Run


def steps_handler(mark: str, plan: list[tuple[str, dict]], final: str):
    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        names, done = tool_names(body), [n for n, _ in called(body)]
        for name, args in plan:
            if name not in done:
                if name not in names and "tools_load" in names and "tools_load" not in done:
                    return ("tools_load", {"groups": ["files", "telegram", "tasks"]})
                return (name, args)
        return final
    return handler


async def run_chat(mock_llm, text: str, mark: str, plan, final: str, telegram=None) -> Run:
    from xagent_agent.pipeline import AgentRunner

    async with session_scope() as s:
        run = Run(trigger="chat", input=f"{text} ({mark})", status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    h = steps_handler(mark, plan, final)
    mock_llm.handlers.insert(0, h)
    try:
        await AgentRunner("s3", telegram=telegram).execute(rid)
    finally:
        mock_llm.handlers.remove(h)
    async with session_scope() as s:
        return await s.get(Run, rid)


async def _seed_file():
    from xagent_core.config import get_settings

    p = get_settings().workspace_dir / "notes"
    p.mkdir(parents=True, exist_ok=True)
    (p / "f07.txt").write_text("یادداشت آزمون")


async def test_F07_irrelevant_file_read_never_proves_telegram_send(app_client, configured_model, mock_llm):
    await _seed_file()
    fake = FakeTelegram()
    run = await run_chat(mock_llm, "به علی در تلگرام پیام بده: گزارش آماده است", "F07-SEND-FAKE",
                         [("file_read", {"path": "notes/f07.txt"})], "پیام به علی ارسال شد.", telegram=fake)
    assert not fake.sent
    assert run.status != "succeeded", "خواندن فایل بی‌ربط به‌عنوان اثبات ارسال پیام پذیرفته شد"
    assert (run.state.get("objective") or {}).get("missing") == ["message_sent"]


async def test_F07_verified_send_completes_send_objective(app_client, configured_model, mock_llm):
    from xagent_core.settings_store import TelegramSettings, set_setting

    async with session_scope() as s:
        await set_setting(s, "telegram", TelegramSettings(enabled=True, allowlist=["ali"]))
    from sqlalchemy import select

    from xagent_agent.pipeline import AgentRunner
    from xagent_core.models import Approval

    fake = FakeTelegram()
    plan = [("telegram_send", {"chat": "ali", "text": "گزارش آماده است"})]
    run = await run_chat(mock_llm, "به علی در تلگرام پیام بده: گزارش آماده است", "F07-SEND-OK", plan,
                         "پیام ارسال شد.", telegram=fake)
    assert run.status == "waiting_approval", run.status  # ارسال بیرونی: مسیر واقعی تأیید کارفرما
    async with session_scope() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == run.id))).scalar_one()
    assert (await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "approve"})).status_code == 200
    h = steps_handler("F07-SEND-OK", plan, "پیام ارسال شد.")
    mock_llm.handlers.insert(0, h)
    try:
        await AgentRunner("s3", telegram=fake).execute(run.id)
    finally:
        mock_llm.handlers.remove(h)
    async with session_scope() as s:
        run = await s.get(Run, run.id)
    assert run.status == "succeeded", run.error
    assert run.state["objective"]["completed"] == ["message_sent"] and fake.sent


async def test_F07_write_and_task_objectives_need_relevant_verified_effects(app_client, configured_model, mock_llm):
    await _seed_file()
    w = await run_chat(mock_llm, "یک فایل یادداشت بساز با متن سلام", "F07-WRITE-FAKE",
                       [("file_list", {"path": "."})], "فایل ساخته شد.")
    assert w.status != "succeeded", "فهرست فایل‌ها به‌عنوان ساخت فایل پذیرفته شد"
    t = await run_chat(mock_llm, "یک تسک بساز برای تماس با مشتری", "F07-TASK-FAKE",
                       [("task_list", {})], "تسک ساخته شد.")
    assert t.status != "succeeded", "فهرست تسک‌ها به‌عنوان ساخت تسک پذیرفته شد"
    ok = await run_chat(mock_llm, "یک تسک بساز برای تماس با مشتری", "F07-TASK-OK",
                        [("task_create", {"title": "تماس با مشتری"})], "تسک «تماس با مشتری» ساخته شد.")
    assert ok.status == "succeeded" and ok.state["objective"]["completed"] == ["task_created"], ok.error


async def test_F07_multi_step_partial_completion_is_not_success(app_client, configured_model, mock_llm):
    fake = FakeTelegram()
    run = await run_chat(mock_llm, "یک فایل یادداشت بساز و به علی در تلگرام پیام بده", "F07-MULTI",
                         [("file_write", {"path": "notes/f07-multi.txt", "content": "x"})], "هر دو انجام شد.",
                         telegram=fake)
    obj = run.state.get("objective") or {}
    assert run.status != "succeeded" and obj.get("status") == "partial", obj
    assert obj.get("completed") == ["file_written"] and obj.get("missing") == ["message_sent"]


async def test_F07_reading_and_plain_conversation_need_no_effect(app_client, configured_model, mock_llm):
    await _seed_file()
    r = await run_chat(mock_llm, "فایل notes/f07.txt را بخوان", "F07-READ",
                       [("file_read", {"path": "notes/f07.txt"})], "متن فایل: یادداشت آزمون")
    assert r.status == "succeeded", r.error
    c = await run_chat(mock_llm, "سلام", "F07-HI", [], "سلام! چطور کمک کنم؟")
    assert c.status == "succeeded" and (c.state.get("objective") or {}).get("status") in (None, "not_required")
