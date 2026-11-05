"""مأموریت جامع — مرحله ۷: بودجه کامل درخواست مدل (A8)."""

from __future__ import annotations

import json
import uuid

from autonomy_helpers import body_text, called, tool_names
from sqlalchemy import select

from xagent_agent.pipeline import TOOL_OUTPUT_MAX_CHARS, bound_tool_output, fit_request
from xagent_agent.providers.base import LLMMessage, ToolCall, ToolSpec
from xagent_core.db import session_scope
from xagent_core.models import AIModel, Conversation, Message, Run
from xagent_core.text import estimate_tokens


# ── مورد دقیق ممیزی: پیام ۴۰٬۰۰۰ نویسه‌ای با بودجه ۱۰۰ توکن ──
async def test_A8_first_message_never_exceeds_window_budget(app_client):
    from xagent_memory.working import load_window

    big = ("گزارش طولانی " * 4000)[:40_000]
    async with session_scope() as s:
        conv = Conversation(title="A8", channel="web")
        s.add(conv)
        await s.flush()
        s.add(Message(conversation_id=conv.id, role="user", content=big))
        await s.flush()
        cid = conv.id
    async with session_scope() as s:
        summary, msgs = await load_window(s, cid, budget_tokens=100)
        used = sum(estimate_tokens(m.content) for m in msgs) + estimate_tokens(summary or "")
    assert used <= 100, f"پنجره کاری {used} توکن با بودجه ۱۰۰"
    assert msgs and "حذف شد" in msgs[0].content
    async with session_scope() as s:
        stored = (await s.execute(select(Message.content).where(Message.conversation_id == cid))).scalar_one()
    assert stored == big, "متن ذخیره‌شده پیام تغییر کرد"


def _pairs(n: int, size: int) -> list[LLMMessage]:
    tx: list[LLMMessage] = []
    for i in range(n):
        tx.append(LLMMessage("assistant", "", tool_calls=[ToolCall(f"c{i}", "file_read", {"path": f"f{i}.txt"})]))
        tx.append(LLMMessage("tool", f"[{i}]" + "x" * size, tool_call_id=f"c{i}", tool_name="file_read"))
    return tx


def test_A8_multistep_large_tool_outputs_fit_and_keep_pairing():
    specs = [ToolSpec("file_read", "خواندن فایل", {"type": "object", "properties": {"path": {"type": "string"}}})]
    tx = _pairs(30, 20_000)
    hist = [LLMMessage("user", "قبلی " * 2000), LLMMessage("assistant", "پاسخ " * 2000)]
    user = LLMMessage("user", "همه فایل‌ها را بخوان و خلاصه کن")
    h, u, t, stats = fit_request("دستور سیستم ثابت", specs, hist, user, tx, max_tokens=4096, window=32_000)
    assert stats["changed"] and stats["fits"], stats
    assert stats["est_after"] <= stats["budget"]
    ids = {c.id for m in t if m.role == "assistant" for c in m.tool_calls}
    assert ids == {m.tool_call_id for m in t if m.role == "tool"}, "جفت فراخوانی/نتیجه ابزار شکست"
    assert [m.content for m in t[-6:] if m.role == "tool"] == [m.content for m in tx[-6:] if m.role == "tool"]
    assert len(tx[1].content) > 20_000, "رونوشت اصلی (State) تغییر کرد"


def test_A8_huge_user_message_is_clipped_with_marker():
    user = LLMMessage("user", "متن " * 100_000)
    _h, u, _t, stats = fit_request("sys", [], [], user, [], max_tokens=4096, window=16_000)
    assert stats["fits"] and "حذف شد" in u.content and estimate_tokens(u.content) < 16_000


def test_A8_large_tool_output_kept_retrievable(tmp_path, monkeypatch):
    from xagent_core.config import get_settings

    out = "A" * 30_000 + "MIDDLE-SECRET-MARKER" + "B" * 30_000
    rid = uuid.uuid4()
    bounded = bound_tool_output(rid, "call-1", out)
    assert len(bounded) < TOOL_OUTPUT_MAX_CHARS and "MIDDLE-SECRET-MARKER" not in bounded
    # نام فایل از هش شناسه فراخوانی ساخته می‌شود (F10)؛ ارجاع درج‌شده در رونوشت باید همان متن کامل را بدهد
    rel = bounded.split("متن کامل: ", 1)[1].split(" ", 1)[0]
    assert rel.startswith(f"runs/{rid}/")
    ref = get_settings().workspace_dir / rel
    assert ref.read_text(encoding="utf-8") == out


# ── F5 در Pipeline واقعی: چند خواندن فایل بزرگ با پنجره کوچک مدل — هر درخواست در بودجه می‌ماند ──
async def test_A8_pipeline_requests_stay_within_model_window(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner
    from xagent_core.config import get_settings

    ws = get_settings().workspace_dir / "ctx"
    ws.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (ws / f"big{i}.txt").write_text(f"فایل {i}\n" + ("داده آزمایشی " * 6000), encoding="utf-8")
    mark = "A8-PIPE"
    sizes: list[int] = []

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        sizes.append(len(json.dumps(body.get("messages", []), ensure_ascii=False)) // 4)
        if "file_read" not in tool_names(body):
            return ("tools_load", {"groups": ["files"]})
        done = [a.get("path") for n, a in called(body) if n == "file_read"]
        for i in range(4):
            if f"ctx/big{i}.txt" not in done:
                return ("file_read", {"path": f"ctx/big{i}.txt"})
        return "چهار فایل خوانده شد."
    async with session_scope() as s:
        m = await s.get(AIModel, uuid.UUID(configured_model))
        old_window, m.context_window = m.context_window, 24_000
        run = Run(trigger="chat", input=f"چهار فایل بزرگ را بخوان ({mark})", status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("ctx").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
        async with session_scope() as s:
            (await s.get(AIModel, uuid.UUID(configured_model))).context_window = old_window
    budget = int(24_000 * 0.85) - 4096
    assert len(sizes) >= 5
    assert max(sizes) <= budget, f"بزرگ‌ترین درخواست {max(sizes)} توکن با بودجه {budget}"
    async with session_scope() as s:
        run = await s.get(Run, rid)
    assert (run.state.get("context_fit") or {}).get("changed")
