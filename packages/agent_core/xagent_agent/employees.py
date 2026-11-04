"""کارمندان هوش مصنوعی: تفویض کار از مدیر، اجرای مستقل با مدل و ابزار اختصاصی، گزارش و آمار."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from xagent_core import bus
from xagent_core.db import get_sessionmaker, utcnow
from xagent_core.models import Conversation, Employee, Message, Run, RunStep
from xagent_core.text import truncate
from xagent_tools.base import ToolContext, ToolResult


async def delegate(ctx: ToolContext, employee_name: str, task: str, expected_output: str = "") -> ToolResult:
    if ctx.extra.get("employee_name"):
        return ToolResult(False, "کارمند نمی‌تواند کار را دوباره تفویض کند؛ خودت انجامش بده.")
    if int(ctx.extra.get("depth", 0)) >= 1:
        return ToolResult(False, "عمق تفویض مجاز تمام شده است.")
    runner = ctx.extra.get("runner")
    emp = (await ctx.session.execute(select(Employee).where(func.lower(Employee.name) == employee_name.lower()))
           ).scalar_one_or_none()
    if emp is None:
        names = (await ctx.session.execute(select(Employee.name).where(Employee.enabled.is_(True)))).scalars().all()
        return ToolResult(False, f"کارمند «{employee_name}» وجود ندارد. کارمندان: {', '.join(names) or 'هیچ'}؛ "
                                 "با employee_upsert بساز.")
    if not emp.enabled:
        return ToolResult(False, f"کارمند «{emp.name}» غیرفعال است.")
    parent = await ctx.session.get(Run, ctx.run_id) if ctx.run_id else None
    parent_task = parent.task_id if parent else None
    # قفل‌های تراکنش مدیر قبل از اجرای کارمند آزاد می‌شوند (جلوگیری از بن‌بست)
    await ctx.session.commit()
    text = task + (f"\n\nخروجی مورد انتظار: {expected_output}" if expected_output else "")
    # اجرای فرزند در نشست مستقل (Checkpoint و گزارش جدا)
    async with get_sessionmaker()() as s2:
        conv = Conversation(title=f"کارمند {emp.name}: {truncate(task, 50)}", channel="system",
                            external_ref=f"employee:{emp.id}")
        s2.add(conv)
        await s2.flush()
        s2.add(Message(conversation_id=conv.id, role="user", content=text, meta={"from": "manager"}))
        child = Run(conversation_id=conv.id, parent_run_id=ctx.run_id, employee_id=emp.id, trigger="delegate",
                    input=text, task_id=parent_task,
                    state={"trusted": ctx.trusted_owner, "principal": "employee", "requested_by": f"manager:{ctx.run_id}",
                           "employee_id": str(emp.id), "depth": 1,
                           "max_steps": emp.max_steps, "max_cost": float(emp.max_cost_usd or 0.2),
                           "route": {"complexity": "simple", "risk": "low", "path": "worker", "command": None,
                                     "needs_vision": False, "needs_audio": False, "tool_groups": list(emp.tool_groups or []),
                                     "memory_layers": ["behavioral", "semantic", "experiential"],
                                     "reasons": [f"تفویض به کارمند {emp.name}"]},
                           "goal": {"goal": truncate(task, 300), "success_criteria": [expected_output or "انجام کامل کار"]},
                           "plan": {"steps": [{"title": "اجرای کار سپرده‌شده", "tool_hint": "auto"}],
                                    "tool_groups": list(emp.tool_groups or []),
                                    "memory_layers": ["behavioral", "semantic", "experiential"]}})
        s2.add(child)
        e2 = await s2.get(Employee, emp.id)
        e2.status = "working"
        await s2.commit()
        child_id = child.id
    await bus.publish("employee.status", {"id": str(emp.id), "name": emp.name, "status": "working", "run_id": str(child_id)})
    if runner is None:
        from .pipeline import AgentRunner

        runner = AgentRunner("delegate")
    await runner.execute(child_id)
    async with get_sessionmaker()() as s3:
        r = await s3.get(Run, child_id)
        tools = (await s3.execute(select(RunStep.tool_name, RunStep.status).where(
            RunStep.run_id == child_id, RunStep.kind == "tool"))).all()
        e3 = await s3.get(Employee, emp.id)
        st: dict[str, Any] = dict(e3.stats or {})
        ok = r.status == "succeeded"
        st["runs"] = st.get("runs", 0) + 1
        st["success"] = st.get("success", 0) + (1 if ok else 0)
        st["failures"] = st.get("failures", 0) + (0 if ok else 1)
        st["tokens"] = st.get("tokens", 0) + r.tokens_in + r.tokens_out
        st["cost"] = round(float(Decimal(str(st.get("cost", 0))) + Decimal(r.cost_usd or 0)), 6)
        st["last_run_at"] = utcnow().isoformat()
        e3.stats = st
        e3.status = "idle" if ok else "error"
        await s3.commit()
        report = (f"گزارش کارمند «{emp.name}» ({emp.title}) — وضعیت: {r.status}\n"
                  f"مصرف: {r.tokens_in + r.tokens_out} توکن، ${float(r.cost_usd or 0):.5f}، {r.steps_count} اقدام\n"
                  f"ابزارها: {', '.join(f'{n}({st_})' for n, st_ in tools) or 'هیچ'}\n"
                  f"نتیجه:\n{truncate(r.result or r.error or '', 6000)}\n\n"
                  "این نتیجه را با معیار کار ارزیابی کن؛ اگر ناقص است، با بازخورد دقیق دوباره تفویض کن یا خودت تکمیل کن.")
    await bus.publish("employee.status", {"id": str(emp.id), "name": emp.name, "status": e3.status, "run_id": str(child_id)})
    return ToolResult(ok, report, {"run_id": str(child_id), "status": r.status}, verified=ok)


async def ensure_ids(values: list[Any]) -> list[str]:
    return [str(uuid.UUID(str(v))) for v in values]
