"""تنظیمات عملیاتی ذخیره‌شده در دیتابیس با اسکیمای Pydantic و مقادیر پیش‌فرض امن."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SettingKV


class GeneralSettings(BaseModel):
    agent_name: str = "X Agent"
    owner_name: str = "کارفرما"
    timezone: str = "Asia/Tehran"
    calendar: Literal["jalali", "gregorian"] = "jalali"
    language: str = "fa"
    persona: str = "دستیار و کارمند دیجیتال حرفه‌ای، دقیق، مؤدب و کم‌حرف."


class SmartModeSettings(BaseModel):
    manager_model_ids: list[str] = Field(default_factory=list)  # حداکثر ۳ مدل به ترتیب اولویت
    worker_model_ids: list[str] = Field(default_factory=list)
    manager_enabled: list[bool] = Field(default_factory=list)
    worker_enabled: list[bool] = Field(default_factory=list)


PRO_ROLES = [
    "management",
    "goal",
    "planning",
    "execution",
    "vision",
    "stt",
    "tts",
    "memory",
    "evaluation",
    "dream",
]


class AISettings(BaseModel):
    mode: Literal["smart", "pro"] = "smart"
    smart: SmartModeSettings = Field(default_factory=SmartModeSettings)
    pro: dict[str, str | None] = Field(default_factory=lambda: {r: None for r in PRO_ROLES})
    embedding_model_id: str | None = None  # None → جاسازی محلی (hash) بدون هزینه
    temperature: float = 0.3
    tts_voice: str = "alloy"


class StopLossSettings(BaseModel):
    max_cost_per_task_usd: float = 0.5
    max_tokens_per_task: int = 200_000
    daily_cost_usd: float = 5.0
    monthly_cost_usd: float = 60.0
    max_steps_simple: int = 6
    max_steps_complex: int = 25
    max_runtime_seconds: int = 900
    max_repeat_same_action: int = 2
    max_consecutive_errors: int = 3


class RoutineSafetySettings(BaseModel):
    circuit_failure_threshold: int = 3
    circuit_cooldown_minutes: int = 30
    lease_ttl_seconds: int = 600


class DreamSettings(BaseModel):
    enabled: bool = True
    hour: int = 3
    minute: int = 30
    max_cost_usd: float = 0.3
    min_events_for_llm: int = 3
    probation_runs: int = 3
    regression_token_ratio: float = 1.3  # افزایش ۳۰٪ توکن = پسرفت


class TelegramSettings(BaseModel):
    enabled: bool = False
    api_id: int | None = None
    api_hash_set: bool = False
    phone: str | None = None
    allowlist: list[str] = Field(default_factory=list)  # chat id یا username
    owner_chat: str | None = None  # گفتگوی کارفرما برای فرمان دادن به ایجنت
    respond_to_owner_only: bool = True
    auto_link_crm: bool = True


class EmbeddingSettings(BaseModel):
    provider: Literal["local_hash", "model"] = "local_hash"


DEFAULTS: dict[str, type[BaseModel]] = {
    "general": GeneralSettings,
    "ai": AISettings,
    "stoploss": StopLossSettings,
    "routine_safety": RoutineSafetySettings,
    "dream": DreamSettings,
    "telegram": TelegramSettings,
}


async def get_setting[T: BaseModel](session: AsyncSession, key: str, model: type[T]) -> T:
    row = await session.get(SettingKV, key)
    if row is None:
        return model()
    try:
        return model.model_validate(row.value)
    except Exception:
        return model()


async def set_setting(session: AsyncSession, key: str, value: BaseModel | dict[str, Any]) -> None:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    row = await session.get(SettingKV, key)
    if row is None:
        session.add(SettingKV(key=key, value=data))
    else:
        row.value = data
    await session.flush()


async def get_raw(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.get(SettingKV, key)
    return row.value if row else None


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rows = (await session.execute(select(SettingKV))).scalars().all()
    stored = {r.key: r.value for r in rows}
    for key, model in DEFAULTS.items():
        try:
            out[key] = model.model_validate(stored.get(key, {})).model_dump(mode="json")
        except Exception:
            out[key] = model().model_dump(mode="json")
    return out
