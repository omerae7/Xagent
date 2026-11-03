"""پیکربندی مرکزی X Agent.

همه مقادیر از متغیرهای محیطی (فایل `.env` که install.sh می‌سازد) خوانده می‌شوند.
پروفایل سخت‌افزاری (lite/standard/performance) توسط نصاب تشخیص داده می‌شود و
اندازه استخرها، تعداد Workerها و محدودیت‌ها را تنظیم می‌کند.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Profile = Literal["lite", "standard", "performance"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAGENT_", env_file=".env", extra="ignore")

    env: Literal["production", "development", "test"] = "production"
    profile: Profile = "standard"

    # کلید اصلی: برای امضای JWT و رمزنگاری کلیدهای API استفاده می‌شود (Fernet مشتق‌شده)
    secret_key: str = Field(default="change-me-in-production-please-32+chars")

    database_url: str = "postgresql+asyncpg://xagent:xagent@localhost:5432/xagent"
    redis_url: str = "redis://localhost:6379/0"

    data_dir: Path = Path("/data")
    public_url: str = "http://localhost"
    default_timezone: str = "Asia/Tehran"

    # مالک اولیه (فقط در اولین اجرا برای ساخت حساب استفاده می‌شود)
    owner_username: str = "admin"
    owner_password: str | None = None

    # کامپیوتر اختصاصی ایجنت (کانتینر دسکتاپ)
    desktop_url: str = "http://desktop:7000"
    desktop_vnc_ws_view: str = "ws://desktop:6080/websockify"
    desktop_vnc_ws_control: str = "ws://desktop:6081/websockify"
    desktop_cdp_url: str = "http://desktop:9222"
    desktop_token: str = "change-me-desktop-token"

    # به‌روزرسانی از GitHub
    repo_url: str = "https://github.com/omerae7/Xagent.git"
    repo_branch: str = "main"
    updater_url: str = "http://updater:7100"

    # جاسازی (embedding) — اندازه ثابت ستون pgvector
    embedding_dim: int = 768

    access_token_minutes: int = 60 * 24 * 7
    cookie_secure: bool = True

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def tools_dir(self) -> Path:
        return self.data_dir / "tools"

    @property
    def db_pool_size(self) -> int:
        return {"lite": 4, "standard": 10, "performance": 20}[self.profile]

    @property
    def max_concurrent_runs(self) -> int:
        cpus = os.cpu_count() or 2
        base = {"lite": 1, "standard": 3, "performance": 6}[self.profile]
        return max(1, min(base, cpus))


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.workspace_dir, s.uploads_dir, s.tools_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return s
