"""ممیزی جرم‌شناختی — مرحله ۳، F22: درخواستی که می‌دانیم از ظرفیت مدل بیشتر است هرگز ارسال نمی‌شود."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from autonomy_helpers import body_text

from xagent_core.db import session_scope
from xagent_core.models import AIModel, Run


async def test_F22_oversized_execution_request_never_reaches_provider(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    async with session_scope() as s:
        m = await s.get(AIModel, __import__("uuid").UUID(configured_model))
        old_window, m.context_window = m.context_window, 3000  # کوچک‌تر از دستور سیستم + Schema ابزارها + خروجی
    mark = "F22-CAP"
    try:
        async with session_scope() as s:
            run = Run(trigger="chat", input=f"یک فایل یادداشت بساز با متن سلام ({mark})", status="queued",
                      state={"trusted": True})
            s.add(run)
            await s.flush()
            rid = run.id
        before = len(mock_llm.calls)
        await AgentRunner("s3c").execute(rid)
        sent = [c for c in mock_llm.calls[before:] if c.get("tools") and mark in body_text(c)]
        # بودجه این پنجره = ۳۰۰۰×۰٫۸۵ − ۴۰۹۶ (منفی): هیچ درخواست اجرایی جا نمی‌شود و نباید ارسال شود
        assert not sent, f"{len(sent)} درخواست اجرایی خارج از ظرفیت مدل به ارائه‌دهنده ارسال شد " \
                         f"(~{max(len(json.dumps(c, ensure_ascii=False)) // 4 for c in sent)} توکن)"
        async with session_scope() as s:
            r = await s.get(Run, rid)
        assert r.status == "failed" and r.stop_reason == "capacity", (r.status, r.stop_reason, r.error)
        assert "ظرفیت" in (r.error or "")
    finally:
        async with session_scope() as s:
            (await s.get(AIModel, __import__("uuid").UUID(configured_model))).context_window = old_window


def _spec(name: str, size: int):
    from xagent_agent.providers.base import ToolSpec

    return ToolSpec(name, "d" * 50, {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "x" * 40}
                                                                      for i in range(size)}})


def test_F22_optional_tool_schemas_dropped_but_used_tools_kept():
    from xagent_agent.pipeline import plan_submission
    from xagent_agent.providers.base import LLMMessage, ToolCall

    specs = [_spec(f"t{i}", 40) for i in range(30)] + [_spec("file_read", 2)]
    tx = [LLMMessage("assistant", "", tool_calls=[ToolCall("c1", "file_read", {"path": "a"})]),
          LLMMessage("tool", "ok", tool_call_id="c1", tool_name="file_read")]
    chain = [SimpleNamespace(context_window=9000, label="small")]
    h, u, t, fit, send_specs, send_chain = plan_submission("system", specs, [], LLMMessage("user", "x"), tx, 1000,
                                                           chain, {"file_read"})
    assert fit["fits"] and [sp.name for sp in send_specs] == ["file_read"] and fit["dropped_tools"] == 30


def test_F22_larger_authorized_model_used_otherwise_explicit_capacity_failure():
    from xagent_agent.pipeline import CapacityExceeded, plan_submission
    from xagent_agent.providers.base import LLMMessage

    big_system = "قانون امنیتی " * 6000  # دستور سیستم/امنیت کوتاه نمی‌شود
    chain = [SimpleNamespace(context_window=8000, label="small"), SimpleNamespace(context_window=200_000, label="big")]
    *_, fit, _specs, send_chain = plan_submission(big_system, [], [], LLMMessage("user", "x"), [], 1000, chain, set())
    assert fit["fits"] and [m.label for m in send_chain] == ["big"]
    with pytest.raises(CapacityExceeded):
        plan_submission(big_system, [], [], LLMMessage("user", "x"), [], 1000, chain[:1], set())
