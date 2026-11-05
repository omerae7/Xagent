"""ممیزی جرم‌شناختی — مرحله ۴، F09: بودجه ریشه بر کل درخت اجرا (والد، فرزند، نوه، هم‌سطح).

هزینه‌ها ساختگی‌اند (مدل شبیه‌ساز با قیمت ثابت)؛ هیچ تماس واقعی با ارائه‌دهنده انجام نمی‌شود.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from xagent_core.db import session_scope
from xagent_core.models import Run, UsageEvent


def model(per_call: str):
    return SimpleNamespace(id=uuid.uuid4(), provider_id=uuid.uuid4(), provider_kind="custom", model_name="mock-f09",
                           price_estimated=False, cost=lambda i, o, cr, cw: Decimal(per_call))


def req(max_tokens: int = 100):
    from xagent_agent.providers.base import ChatRequest, LLMMessage

    return ChatRequest(model="mock-f09", system_stable="", system_dynamic="", messages=[LLMMessage("user", "x")],
                       max_tokens=max_tokens)


async def tree(root_cap: str | None) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """ریشه (سقف اختیاری) ← فرزند ← نوه، و یک فرزند هم‌سطح."""
    async with session_scope() as s:
        root = Run(trigger="chat", input="ریشه", status="running",
                   state={"trusted": True, **({"max_cost": root_cap} if root_cap else {})})
        s.add(root)
        await s.flush()
        child = Run(trigger="delegate", input="فرزند", status="running", state={}, parent_run_id=root.id)
        sib = Run(trigger="delegate", input="هم‌سطح", status="running", state={}, parent_run_id=root.id)
        s.add_all([child, sib])
        await s.flush()
        grand = Run(trigger="delegate", input="نوه", status="running", state={}, parent_run_id=child.id)
        s.add(grand)
        await s.flush()
        return root.id, child.id, sib.id, grand.id


async def usage(run_id, *, cost="0", reserved="0", status="confirmed", tokens=0, reserved_tokens=0):
    async with session_scope() as s:
        s.add(UsageEvent(run_id=run_id, purpose="execution", attempt_status=status, success=status == "confirmed",
                         cost_usd=Decimal(cost), reserved_cost_usd=Decimal(reserved), tokens_in=tokens,
                         reserved_tokens=reserved_tokens, model_name="mock-f09", provider_kind="custom"))


async def test_F09_tighter_root_budget_covers_grandchildren_and_unresolved_exposure(app_client):
    from xagent_agent.usage_ledger import BudgetDenied, begin_attempt

    root, child, sib, grand = await tree(root_cap="0.05")
    await usage(root, cost="0.02")                                   # مصرف تأییدشده
    await usage(child, reserved="0.015", status="pending")           # رزرو حل‌نشده
    await usage(sib, reserved="0.01", status="unknown")              # هزینه نامعلوم (نگه داشته می‌شود)
    with pytest.raises(BudgetDenied):
        await begin_attempt(model("0.01"), req(), purpose="execution", run_id=grand)  # 0.045 + 0.01 > 0.05
    await begin_attempt(model("0.004"), req(), purpose="execution", run_id=grand)     # 0.049 ≤ 0.05: مجاز


async def test_F09_concurrent_child_reservations_never_exceed_root_budget(app_client):
    from xagent_agent.usage_ledger import BudgetDenied, begin_attempt

    root, child, sib, _ = await tree(root_cap="0.05")
    results = await asyncio.gather(*[begin_attempt(model("0.03"), req(), purpose="execution", run_id=r)
                                     for r in (child, sib)], return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    denied = [r for r in results if isinstance(r, BudgetDenied)]
    assert len(ok) == 1 and len(denied) == 1, f"رزرو هم‌زمان فرزندان از بودجه ریشه گذشت: {results}"


async def test_F09_tree_token_limit_and_root_without_cap_uses_global(app_client):
    from xagent_agent.usage_ledger import BudgetDenied, begin_attempt
    from xagent_core.settings_store import StopLossSettings, get_setting

    async with session_scope() as s:
        lim = (await get_setting(s, "stoploss", StopLossSettings)).max_tokens_per_task
    root, child, _, grand = await tree(root_cap=None)
    await usage(child, tokens=lim - 50)
    with pytest.raises(BudgetDenied):
        await begin_attempt(model("0.0001"), req(max_tokens=200), purpose="execution", run_id=grand)
