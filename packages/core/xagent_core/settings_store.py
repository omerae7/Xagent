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
    # مسیریابی مدل در حالت اسمارت: custom = دقیقاً زنجیره و ترتیب کارفرما (رفتار قبلی، پیش‌فرض برای حفظ تنظیمات)
    # economy | balanced | quality = انتخاب خودکار از میان مدل‌های فعال بر اساس هزینه کل مورد انتظار، کیفیت، تأخیر و سلامت
    routing_policy: Literal["economy", "balanced", "quality", "custom"] = "custom"
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
    # بودجه اجرایی تطبیقی: مأموریت‌های تعاملی (مرورگر/دسکتاپ) پایه بزرگ‌تری دارند و در صورت
    # پیشرفت واقعی (مشاهده جدید، بدون خطای پیاپی، هزینه زیر سقف) حداکثر step_extensions بار تمدید
    # می‌شوند؛ هیچ تمدیدی از max_steps_hard، سقف هزینه یا سقف کارمند عبور نمی‌کند.
    max_steps_interactive: int = Field(30, ge=1, le=200)
    max_steps_hard: int = Field(60, ge=1, le=300)
    step_extensions: int = Field(2, ge=0, le=5)
    no_progress_limit: int = Field(6, ge=2, le=50)
    max_runtime_seconds: int = 900
    max_repeat_same_action: int = 2
    max_consecutive_errors: int = 3
    # سقف غیرقابل‌تجاوز هر کار کارمند (مدیر فقط می‌تواند کمتر تعیین کند)
    employee_max_cost_usd: float = 0.3
    employee_max_steps: int = 15
    # قیمت محافظه‌کارانه (دلار به ازای هر میلیون توکن) برای مدل‌هایی که قیمت معتبر ندارند
    unpriced_input_per_m: float = 15.0
    unpriced_output_per_m: float = 75.0


class RoutineSafetySettings(BaseModel):
    circuit_failure_threshold: int = 3
    circuit_cooldown_minutes: int = 30
    lease_ttl_seconds: int = 600


class DreamSettings(BaseModel):
    # Paid background activity is opt-in; migration 0004 disables prior schedules.
    enabled: bool = False
    hour: int = Field(3, ge=0, le=23)
    minute: int = Field(30, ge=0, le=59)
    max_cost_usd: float = Field(0.3, gt=0)
    max_tokens: int = Field(12_000, ge=1000, le=100_000)
    max_calls: int = Field(2, ge=1, le=5)
    max_runtime_seconds: int = Field(300, ge=30, le=1800)
    min_events_for_llm: int = Field(3, ge=1, le=1000)
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


class HostSettings(BaseModel):
    """سیاست کارفرما برای کارگزار فرمان میزبان (فقط کارفرما تغییر می‌دهد)."""

    enabled: bool = True
    # حالت هر عملیات: auto (پیش‌فرض سطح ۱ و ۲) | ask (کارت اجازه) | forbidden
    op_modes: dict[str, Literal["auto", "ask", "forbidden"]] = Field(default_factory=dict)
    restart_per_hour: int = Field(3, ge=0, le=20)
    backup_per_hour: int = Field(2, ge=0, le=10)
    prune_per_day: int = Field(2, ge=0, le=10)
    # آپدیت هسته: manual = همیشه با تأیید | auto = فقط از شاخه تنظیم‌شده و در بازه زمانی مشخص
    update_policy: Literal["manual", "auto"] = "manual"
    auto_window_start_hour: int = Field(3, ge=0, le=23)
    auto_window_end_hour: int = Field(5, ge=0, le=23)
    # اعتبار مجوز اجرای فرمان خارج از Allowlist (دقیقه)
    exec_session_minutes: int = Field(15, ge=1, le=60)


class ReportSettings(BaseModel):
    """گزارش‌دهی پیش‌دستانه به میز کار کارفرما."""

    # حداقل شدت برای ارسال هم‌زمان به تلگرام کارفرما (Saved Messages)؛ off یعنی فقط پنل
    telegram_min_severity: Literal["off", "info", "important", "critical"] = "critical"
    # اجرای موفق روتین‌های غیرپایشی (مثلاً «گزارش روزانه») در میز کار ثبت شود
    routine_success: bool = True
    # گفتگوهایی که بیش از این مدت طول بکشند پس از پایان در میز کار گزارش می‌شوند
    long_run_seconds: int = Field(120, ge=10, le=86400)


class CursorSettings(BaseModel):
    """اجراکننده بیرونی Cursor Cloud Agents (فقط کارفرما تغییر می‌دهد). کلید جدا و رمزشده در cursor_secret.

    ایمنی پیش‌فرض: غیرفعال؛ فقط مخازن صریحاً مجاز؛ کار روی شاخه جدید (هرگز مستقیم روی شاخه مبدأ/main)؛
    ساخت PR خودکار خاموش؛ Merge خودکار وجود ندارد؛ هیچ متغیر محیطی یا Secret ارسال نمی‌شود.
    """

    enabled: bool = False
    base_url: str = "https://api.cursor.com"
    allowed_repos: list[str] = Field(default_factory=list)  # https://github.com/owner/repo
    allowed_refs: list[str] = Field(default_factory=list)  # شاخه‌های مبدأ مجاز؛ خالی = شاخه پیش‌فرض مخزن
    auto_create_pr: bool = False
    max_concurrent: int = Field(1, ge=1, le=10)
    max_launches_per_day: int = Field(3, ge=0, le=100)
    model: str = ""  # خالی = مدل پیش‌فرض Cursor
    poll_seconds: int = Field(60, ge=15, le=3600)
    max_runtime_hours: int = Field(6, ge=1, le=72)


class EmbeddingSettings(BaseModel):
    provider: Literal["local_hash", "model"] = "local_hash"


DEFAULTS: dict[str, type[BaseModel]] = {
    "general": GeneralSettings,
    "ai": AISettings,
    "stoploss": StopLossSettings,
    "routine_safety": RoutineSafetySettings,
    "dream": DreamSettings,
    "telegram": TelegramSettings,
    "host": HostSettings,
    "reports": ReportSettings,
    "cursor": CursorSettings,
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
