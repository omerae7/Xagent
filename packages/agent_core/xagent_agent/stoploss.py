"""مجری قطعی حد ضرر (Deterministic Stop-Loss Enforcer) و کنترلر تطبیقی حلقه.

لایه ۱ (LoopController): تشخیص درجا زدن، تکرار یک اقدام و خطای پیاپی → تغییر استراتژی یا ارتقا به مدیر
لایه ۲ (StopLossEnforcer): سقف هزینه/توکن هر تسک، سقف روزانه/ماهانه، تعداد گام و زمان اجرا — سمت سرور
لایه ۳ (Circuit Breaker و قفل اتمیک روتین‌ها) در Worker پیاده شده است.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.models import UsageEvent
from xagent_core.settings_store import StopLossSettings
from xagent_core.timeutil import get_zone, now_utc


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    code: str = ""


async def spend_since(session: AsyncSession, since: datetime) -> Decimal:
    key = f"xagent:spend:{int(since.timestamp())}"
    r = bus.get_redis()
    try:
        cached = await r.get(key)
        if cached is not None:
            return Decimal(cached)
    except Exception:
        cached = None
    total = (
        await session.execute(select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(UsageEvent.ts >= since))
    ).scalar_one()
    try:
        await r.set(key, str(total), ex=20)
    except Exception:
        pass
    return Decimal(total)


async def invalidate_spend_cache() -> None:
    r = bus.get_redis()
    try:
        async for k in r.scan_iter("xagent:spend:*", count=100):
            await r.delete(k)
    except Exception:
        pass


def _period_starts(tz: str) -> tuple[datetime, datetime]:
    local = now_utc().astimezone(get_zone(tz))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return day, month


@dataclass
class StopLossEnforcer:
    settings: StopLossSettings
    timezone: str
    complexity: str
    started_monotonic: float = field(default_factory=time.monotonic)
    max_cost_override: float | None = None
    max_steps_override: int | None = None
    # بودجه اجرایی تطبیقی (به‌جای سقف ثابت): حد فعلی، سقف سخت و تعداد تمدید باقی‌مانده
    step_limit: int | None = None
    hard_limit: int | None = None
    extensions_left: int = 0

    @property
    def max_steps(self) -> int:
        if self.max_steps_override:
            return self.max_steps_override
        if self.step_limit:
            return self.step_limit
        if self.complexity == "complex":
            return self.settings.max_steps_complex
        return self.settings.max_steps_simple

    def configure(self, groups: list[str] | set[str], state: dict | None = None) -> None:
        """تعیین بودجه گام بر اساس نوع مأموریت؛ سقف صریح کارفرما/کارمند هرگز تمدید نمی‌شود."""
        if state:
            self.step_limit = state.get("limit")
            self.hard_limit = state.get("hard")
            self.extensions_left = int(state.get("ext_left", 0))
            return
        base, hard = adaptive_step_budget(self.settings, self.complexity, groups)
        self.step_limit, self.hard_limit = base, hard
        self.extensions_left = self.settings.step_extensions if not self.max_steps_override else 0

    def escalate(self) -> None:
        """ارتقا به مدیر: حداقل بودجه کار پیچیده (بدون عبور از سقف سخت)."""
        if self.step_limit is not None:
            self.step_limit = min(max(self.step_limit, self.settings.max_steps_complex),
                                  self.hard_limit or self.settings.max_steps_complex)

    def try_extend(self, loop: LoopController, cost: Decimal, steps: int) -> bool:
        """تمدید فقط با پیشرفت واقعی، بدون خطای پیاپی، هزینه زیر ۷۰٪ سقف و زیر سقف سخت."""
        if self.max_steps_override or not self.step_limit or self.extensions_left <= 0:
            return False
        hard = self.hard_limit or self.step_limit
        if steps >= hard or float(cost) >= 0.7 * self.max_cost:
            return False
        if loop.consecutive_errors > 0 or loop.progress_ratio() < 0.5:
            return False
        grow = max(4, self.step_limit // 2)
        self.step_limit = min(hard, self.step_limit + grow)
        self.extensions_left -= 1
        return True

    def budget_state(self) -> dict:
        return {"limit": self.step_limit, "hard": self.hard_limit, "ext_left": self.extensions_left}

    @property
    def max_cost(self) -> float:
        return self.max_cost_override if self.max_cost_override is not None else self.settings.max_cost_per_task_usd

    async def check(
        self, session: AsyncSession, run_id: str, cost: Decimal, tokens: int, steps: int, elapsed_offset: float = 0
    ) -> Verdict:
        if await bus.is_stop_all():
            return Verdict(False, "توقف اضطراری سراسری (STOP_ALL) فعال است.", "stop_all")
        if await bus.is_run_cancelled(run_id):
            return Verdict(False, "اجرا توسط کارفرما متوقف شد.", "cancelled")
        if float(cost) >= self.max_cost:
            return Verdict(False, f"سقف هزینه این تسک (${self.max_cost:.2f}) پر شد.", "task_cost")
        if tokens >= self.settings.max_tokens_per_task:
            return Verdict(False, f"سقف توکن این تسک ({self.settings.max_tokens_per_task:,}) پر شد.", "task_tokens")
        if steps >= self.max_steps:
            return Verdict(False, f"سقف تعداد گام ({self.max_steps}) برای این نوع کار پر شد.", "steps")
        elapsed = time.monotonic() - self.started_monotonic + elapsed_offset
        if elapsed >= self.settings.max_runtime_seconds:
            return Verdict(False, f"سقف زمان اجرا ({self.settings.max_runtime_seconds} ثانیه) پر شد.", "runtime")
        day, month = _period_starts(self.timezone)
        if float(await spend_since(session, day)) >= self.settings.daily_cost_usd:
            return Verdict(False, f"سقف هزینه روزانه (${self.settings.daily_cost_usd:.2f}) پر شد.", "daily")
        if float(await spend_since(session, month)) >= self.settings.monthly_cost_usd:
            return Verdict(False, f"سقف هزینه ماهانه (${self.settings.monthly_cost_usd:.2f}) پر شد.", "monthly")
        return Verdict(True)


Signal = Literal["ok", "change_strategy", "escalate", "abort"]
INTERACTIVE_GROUPS = {"browser", "desktop"}


def adaptive_step_budget(sl: StopLossSettings, complexity: str, groups: list[str] | set[str]) -> tuple[int, int]:
    """(بودجه پایه، سقف سخت) بر اساس پیچیدگی و نوع ابزار.

    مأموریت مرورگر/دسکتاپ ذاتاً تعاملات زیادی دارد (باز کردن، خواندن، کلیک، تایپ…)؛ سقف ۶ گام
    کار ساده آن را نیمه‌کاره می‌گذاشت. هزینه، توکن، زمان و کنترل حلقه همچنان مستقل اعمال می‌شوند.
    """
    base = sl.max_steps_complex if complexity == "complex" else sl.max_steps_simple
    if INTERACTIVE_GROUPS & set(groups):
        base = max(base, sl.max_steps_interactive)
    hard = max(base, sl.max_steps_hard)
    return base, hard


@dataclass
class LoopController:
    """کنترلر تطبیقی: به‌جای تکرار کورکورانه، استراتژی را عوض یا مدیر را وارد می‌کند."""

    max_repeat: int = 2
    max_consecutive_errors: int = 3
    seen: dict[str, int] = field(default_factory=dict)
    consecutive_errors: int = 0
    escalated: bool = False
    no_progress_steps: int = 0
    interventions: int = 0
    no_progress_limit: int = 6
    recent: list[int] = field(default_factory=list)  # ۱ = گام با مشاهده جدید، ۰ = بدون پیشرفت

    @staticmethod
    def signature(tool: str, args: dict) -> str:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return tool + ":" + hashlib.sha1(blob.encode()).hexdigest()[:12]

    def progress_ratio(self, window: int = 6) -> float:
        tail = self.recent[-window:]
        return (sum(tail) / len(tail)) if tail else 1.0

    def observe(self, tool: str, args: dict, ok: bool, digest: str | None = None) -> Signal:
        """digest = اثرانگشت مشاهده نتیجه؛ تکرار یک اقدام با مشاهده جدید (مثلاً «صفحه بعد») پیشرفت است،
        نه حلقه. فقط تکرار اقدام با مشاهده یکسان یا خطا «بدون پیشرفت» شمرده می‌شود."""
        sig = self.signature(tool, args) + (f"@{digest[:10]}" if digest else "")
        self.seen[sig] = self.seen.get(sig, 0) + 1
        self.consecutive_errors = 0 if ok else self.consecutive_errors + 1
        repeat = self.seen[sig]
        progressed = ok and repeat == 1
        self.recent = (self.recent + [1 if progressed else 0])[-20:]
        self.no_progress_steps = 0 if progressed else self.no_progress_steps + 1
        if repeat > self.max_repeat + 1 or self.consecutive_errors > self.max_consecutive_errors + 1:
            return "abort"
        if self.no_progress_steps >= self.no_progress_limit:
            return "abort"
        if self.consecutive_errors >= self.max_consecutive_errors or repeat > self.max_repeat:
            self.interventions += 1
            if not self.escalated:
                self.escalated = True
                return "escalate"
            return "change_strategy"
        if not ok and self.consecutive_errors >= 2:
            self.interventions += 1
            return "change_strategy"
        return "ok"

    def to_state(self) -> dict:
        return {
            "seen": self.seen,
            "consecutive_errors": self.consecutive_errors,
            "escalated": self.escalated,
            "interventions": self.interventions,
            "recent": self.recent,
            "no_progress": self.no_progress_steps,
        }

    @classmethod
    def from_state(cls, d: dict | None, max_repeat: int, max_errors: int, no_progress_limit: int = 6) -> LoopController:
        lc = cls(max_repeat=max_repeat, max_consecutive_errors=max_errors, no_progress_limit=no_progress_limit)
        if d:
            lc.seen = dict(d.get("seen", {}))
            lc.consecutive_errors = int(d.get("consecutive_errors", 0))
            lc.escalated = bool(d.get("escalated", False))
            lc.interventions = int(d.get("interventions", 0))
            lc.recent = list(d.get("recent", []))
            lc.no_progress_steps = int(d.get("no_progress", 0))
        return lc
