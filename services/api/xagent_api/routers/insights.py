"""مشاهده‌پذیری هزینه و عملکرد مأموریت‌ها — فقط از داده واقعی دفتر مصرف و مراحل اجرا.

هزینه «برآوردی» (مدل بدون قیمت معتبر، با قیمت محافظه‌کارانه) همیشه جدا از هزینه «تأییدشده» گزارش می‌شود.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import Employee, Routine, Run, RunStep, UsageEvent, User

from ..deps import current_user, get_session, parse_uuid

router = APIRouter(prefix="/api", tags=["insights"])

_VERIFIED = func.coalesce(func.sum(case((UsageEvent.cost_estimated.is_(False), UsageEvent.cost_usd), else_=0)), 0)
_ESTIMATED = func.coalesce(func.sum(case((UsageEvent.cost_estimated.is_(True), UsageEvent.cost_usd), else_=0)), 0)


def _row(key: Any, calls, tin, tout, ver, est, lat, ok) -> dict[str, Any]:  # noqa: ANN001
    return {"key": str(key) if key is not None else "—", "calls": int(calls or 0), "tokens_in": int(tin or 0),
            "tokens_out": int(tout or 0), "cost_verified": float(ver or 0), "cost_estimated": float(est or 0),
            "avg_latency_ms": int(lat or 0), "success_rate": float(ok) if ok is not None else None}


@router.get("/usage/breakdown")
async def breakdown(dimension: str = Query("model", pattern="^(model|provider|employee|routine|mission|purpose|day|tool)$"),
                    days: int = Query(7, ge=1, le=365), _: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    since = utcnow() - timedelta(days=days)
    if dimension == "tool":
        rows = (await session.execute(
            select(RunStep.tool_name, func.count(), func.avg(RunStep.duration_ms),
                   func.avg(cast(RunStep.status == "ok", Integer)))
            .where(RunStep.kind == "tool", RunStep.started_at >= since).group_by(RunStep.tool_name)
            .order_by(func.count().desc()).limit(100))).all()
        return {"dimension": "tool", "days": days, "items": [
            {"key": t or "—", "calls": int(n), "avg_latency_ms": int(lat or 0), "success_rate": float(ok or 0)}
            for t, n, lat, ok in rows]}
    agg = (func.count(), func.sum(UsageEvent.tokens_in), func.sum(UsageEvent.tokens_out), _VERIFIED, _ESTIMATED,
           func.avg(UsageEvent.latency_ms), func.avg(cast(UsageEvent.success, Integer)))
    base = select().where(UsageEvent.ts >= since, UsageEvent.attempt_status != "reserved")
    if dimension == "model":
        key = UsageEvent.model_name
        q = base.add_columns(key, *agg)
    elif dimension == "provider":
        key = UsageEvent.provider_kind
        q = base.add_columns(key, *agg)
    elif dimension == "purpose":
        key = UsageEvent.purpose
        q = base.add_columns(key, *agg)
    elif dimension == "day":
        key = func.date_trunc("day", UsageEvent.ts)
        q = base.add_columns(key, *agg)
    elif dimension == "employee":
        key = func.coalesce(Employee.name, "مدیر")
        q = base.add_columns(key, *agg).select_from(UsageEvent).outerjoin(Run, Run.id == UsageEvent.run_id) \
            .outerjoin(Employee, Employee.id == Run.employee_id)
    elif dimension == "routine":
        key = func.coalesce(Routine.name, "بدون روتین")
        q = base.add_columns(key, *agg).select_from(UsageEvent).outerjoin(Run, Run.id == UsageEvent.run_id) \
            .outerjoin(Routine, Routine.id == Run.routine_id)
    else:  # mission
        key = Run.state["mission_id"].astext
        q = base.add_columns(key, *agg).select_from(UsageEvent).outerjoin(Run, Run.id == UsageEvent.run_id)
    rows = (await session.execute(q.group_by(key).order_by(_VERIFIED.desc()).limit(100))).all()
    return {"dimension": dimension, "days": days, "items": [_row(*r) for r in rows],
            "note": "cost_estimated = مدل بدون قیمت معتبر (قیمت محافظه‌کارانه)؛ جدا از هزینه تأییدشده"}


@router.get("/runs/{rid}/metrics")
async def run_metrics(rid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    run = await session.get(Run, parse_uuid(rid))
    if run is None:
        raise HTTPException(404, "اجرا یافت نشد")
    models = (await session.execute(
        select(UsageEvent.model_name, UsageEvent.provider_kind, func.count(), func.sum(UsageEvent.tokens_in),
               func.sum(UsageEvent.tokens_out), _VERIFIED, _ESTIMATED, func.avg(UsageEvent.latency_ms),
               func.sum(case((UsageEvent.success.is_(False), 1), else_=0)))
        .where(UsageEvent.run_id == run.id).group_by(UsageEvent.model_name, UsageEvent.provider_kind))).all()
    tools = (await session.execute(
        select(RunStep.tool_name, func.count(), func.sum(case((RunStep.status == "error", 1), else_=0)))
        .where(RunStep.run_id == run.id, RunStep.kind == "tool").group_by(RunStep.tool_name))).all()
    children = (await session.execute(select(Run.id, Run.status, Run.cost_usd, Run.employee_id).where(
        Run.parent_run_id == run.id))).all()
    st = run.state or {}
    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at and run.started_at else None
    return {
        "run_id": str(run.id), "status": run.status, "outcome": run.outcome, "duration_s": duration,
        "llm_calls": run.llm_calls, "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
        "cost_usd": float(run.cost_usd or 0), "steps": run.steps_count,
        "models": [{"model": m, "provider": p, "calls": int(n), "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
                    "cost_verified": float(v or 0), "cost_estimated": float(e or 0), "avg_latency_ms": int(lat or 0),
                    "errors": int(err or 0)} for m, p, n, ti, to, v, e, lat, err in models],
        "tools": [{"tool": t, "calls": int(n), "errors": int(e or 0)} for t, n, e in tools],
        "delegations": [{"run_id": str(c[0]), "status": c[1], "cost_usd": float(c[2] or 0)} for c in children],
        "recoveries": int(st.get("recoveries", 0)), "fallbacks": sorted((st.get("fallback_used") or {}).keys()),
        "validation": st.get("validation"), "model_routing": None,
    }
