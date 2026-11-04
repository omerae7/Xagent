"""Regression tests for billed attempts surviving agent rollback and bounded Dream.

No paid APIs or production systems are touched. PostgreSQL/Redis are the
existing real test services, and the provider is an isolated local mock.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from xagent_agent.llm import call_llm
from xagent_agent.providers.base import ChatRequest, LLMMessage
from xagent_agent.providers.registry import load_model
from xagent_agent.usage_ledger import begin_attempt, finish_attempt
from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import DreamReport, Episode, UsageEvent
from xagent_core.settings_store import DreamSettings, get_setting, set_setting
from xagent_worker.scheduler import claim_scheduled_dream

pytestmark = pytest.mark.asyncio


async def test_confirmed_usage_survives_later_application_rollback(configured_model, mock_llm):
    await bus.set_stop_all(False)
    marker = f"rollback-{uuid.uuid4()}"
    mid = uuid.UUID(configured_model)
    with pytest.raises(RuntimeError, match="simulated failure"):
        async with session_scope() as session:
            model = await load_model(session, mid)
            assert model is not None
            await call_llm(session, [model], purpose=marker, system_stable="",
                           system_dynamic="", messages=[LLMMessage("user", "سلام")])
            raise RuntimeError("simulated failure")
    async with session_scope() as session:
        rows = (await session.execute(select(UsageEvent).where(
            UsageEvent.purpose == marker))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.attempt_status == "confirmed" and row.success
        assert row.request_id and row.finished_at
        assert row.tokens_in > 0 and row.tokens_out > 0
        assert row.reserved_tokens == 0 and row.reserved_cost_usd == 0


async def test_ambiguous_provider_failure_is_not_free(configured_model):
    await bus.set_stop_all(False)
    marker = f"unknown-{uuid.uuid4()}"
    async with session_scope() as session:
        model = await load_model(session, configured_model)
        rid = await begin_attempt(model, ChatRequest(
            model=model.model_name, system_stable="", system_dynamic="",
            messages=[LLMMessage("user", "test")], max_tokens=32), purpose=marker)
    assert await finish_attempt(rid, error=TimeoutError("response lost")) == "unknown"
    # Second completion must not duplicate or overwrite a financial record.
    assert await finish_attempt(rid, error=TimeoutError("duplicate")) == "unknown"
    async with session_scope() as session:
        row = await session.get(UsageEvent, rid)
        assert row.attempt_status == "unknown"
        assert row.reserved_tokens > 0 and row.reserved_cost_usd > 0
        assert row.tokens_in == row.tokens_out == 0
        # Clean up a deliberately ambiguous *test-only* reservation.
        await session.delete(row)


async def test_nightly_slot_once_even_after_failure(configured_model):
    await bus.set_stop_all(False)
    async with session_scope() as session:
        prior = await get_setting(session, "dream", DreamSettings)
        cfg = prior.model_copy(update={"enabled": True, "hour": 0, "minute": 0})
        await set_setting(session, "dream", cfg)
    try:
        rid = await claim_scheduled_dream()
        assert rid is not None
        assert await claim_scheduled_dream() is None
        async with session_scope() as session:
            report = await session.get(DreamReport, rid)
            assert report.slot_key and report.trigger == "scheduled"
            report.status = "failed"
            report.finished_at = utcnow()
        assert await claim_scheduled_dream() is None
    finally:
        async with session_scope() as session:
            await set_setting(session, "dream", prior)


async def test_stop_all_blocks_new_paid_attempt(configured_model):
    await bus.set_stop_all(True)
    try:
        async with session_scope() as session:
            model = await load_model(session, configured_model)
            with pytest.raises(Exception, match="STOP_ALL"):
                await begin_attempt(model, ChatRequest(
                    model=model.model_name, system_stable="", system_dynamic="",
                    messages=[LLMMessage("user", "test")]), purpose="probe")
    finally:
        await bus.set_stop_all(False)


async def test_dream_post_llm_error_keeps_usage_and_slot(configured_model, mock_llm, monkeypatch):
    """Reproduce: model answered, subsequent Dream postprocessing crashes."""
    import xagent_agent.dream as dream_mod

    await bus.set_stop_all(False)
    async with session_scope() as session:
        prior = await get_setting(session, "dream", DreamSettings)
        await set_setting(session, "dream", prior.model_copy(update={
            "enabled": True, "hour": 0, "minute": 0, "min_events_for_llm": 3,
        }))
        for _ in range(3):
            session.add(Episode(kind="routine", title=f"Dream regression {uuid.uuid4()}",
                                summary="Meaningful verified task", tools=["file_read"],
                                status="success", tokens_in=50, tokens_out=20))
    try:
        rid = await claim_scheduled_dream()
        if rid is None:  # A previous test already claimed today's slot
            async with session_scope() as session:
                report = (await session.execute(select(DreamReport).where(
                    DreamReport.slot_key.like("scheduled:%")).order_by(DreamReport.started_at.desc()))).scalars().first()
                assert report is not None
                rid = report.id
                report.status = "running"

        baseline = 0
        async with session_scope() as session:
            baseline = (await session.execute(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.purpose == "dream"))).scalar_one()

        def crash_after_paid_response(_text):
            raise RuntimeError("post-LLM Dream crash")

        monkeypatch.setattr(dream_mod, "parse_json", crash_after_paid_response)
        with pytest.raises(RuntimeError, match="post-LLM"):
            async with session_scope() as session:
                await dream_mod.run_dream(session, report_id=rid)
        async with session_scope() as session:
            count = (await session.execute(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.purpose == "dream", UsageEvent.attempt_status == "confirmed"))).scalar_one()
            assert count > baseline
            saved = await session.get(DreamReport, rid)
            assert saved is not None  # claim committed before caller's rollback
            saved.status = "failed"
            saved.finished_at = utcnow()
        assert await claim_scheduled_dream() is None
    finally:
        async with session_scope() as session:
            await set_setting(session, "dream", prior)


async def test_idle_scheduler_never_invokes_any_llm(configured_model, mock_llm):
    """24 simulated polling cycles while auto Dream is disabled have no API spend."""
    await bus.set_stop_all(False)
    async with session_scope() as session:
        prior = await get_setting(session, "dream", DreamSettings)
        await set_setting(session, "dream", prior.model_copy(update={"enabled": False}))
    count = len(mock_llm.calls)
    try:
        for _ in range(24):
            assert await claim_scheduled_dream() is None
        assert len(mock_llm.calls) == count
    finally:
        async with session_scope() as session:
            await set_setting(session, "dream", prior)


async def test_manual_dream_is_blocked_by_emergency_stop(configured_model, mock_llm):
    import xagent_agent.dream as dream_mod

    await bus.set_stop_all(True)
    try:
        prior_calls = len(mock_llm.calls)
        with pytest.raises(RuntimeError, match="STOP_ALL"):
            async with session_scope() as session:
                await dream_mod.run_dream(session, force=True)
        assert len(mock_llm.calls) == prior_calls
    finally:
        await bus.set_stop_all(False)
