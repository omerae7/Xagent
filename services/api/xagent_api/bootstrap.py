"""راه‌اندازی اولیه: ساخت حساب کارفرما، مقادیر پیش‌فرض Harness و تنظیمات، و حافظه معنایی پایه."""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select

from xagent_agent.harness import ensure_defaults
from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.db import session_scope
from xagent_core.models import SemanticFact, SettingKV, User
from xagent_core.security import hash_password
from xagent_core.settings_store import DEFAULTS, GeneralSettings, set_setting

log = logging.getLogger("xagent.bootstrap")

AGENT_FACTS = {
    "identity": "X Agent یک کارمند دیجیتال دائمی و ۲۴ساعته است که روی سرور اختصاصی کارفرما اجرا می‌شود و مالکیت کامل داده‌ها با کارفرماست.",
    "duties": "وظایف: پاسخ و پیگیری پیام‌ها، مدیریت تسک‌ها و روتین‌ها، CRM همکاران، کار با وب/مرورگر/دسکتاپ، تلگرام شخصی کارفرما، گزارش‌دهی و یادگیری تدریجی.",
    "principles": "اصول: صداقت در گزارش نتایج، کمترین هزینه و توکن، رعایت Harness و قوانین رفتاری کارفرما، درخواست اجازه برای کارهای حساس.",
}


async def bootstrap() -> None:
    s = get_settings()
    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        if count == 0:
            password = s.owner_password or secrets.token_urlsafe(12)
            session.add(User(username=s.owner_username, password_hash=hash_password(password), role="owner",
                             display_name="کارفرما"))
            if not s.owner_password:
                path = s.data_dir / "initial_password.txt"
                try:
                    path.write_text(f"username: {s.owner_username}\npassword: {password}\n", encoding="utf-8")
                    path.chmod(0o600)
                except OSError:
                    pass
                log.warning("حساب کارفرما ساخته شد: %s؛ رمز اولیه فقط در فایل /data/initial_password.txt ذخیره شد", s.owner_username)
        await ensure_defaults(session)
        for key, model in DEFAULTS.items():
            if await session.get(SettingKV, key) is None:
                value = model()
                if isinstance(value, GeneralSettings):
                    value.timezone = s.default_timezone
                await set_setting(session, key, value)
        existing = (await session.execute(select(func.count()).select_from(SemanticFact).where(
            SemanticFact.section == "agent"))).scalar_one()
        if existing == 0:
            from xagent_memory.store import upsert_fact

            for k, v in AGENT_FACTS.items():
                await upsert_fact(session, "agent", k, v, source="system", reason="مقداردهی اولیه", actor="owner",
                                  origin="panel")
    await bus.ensure_groups()
