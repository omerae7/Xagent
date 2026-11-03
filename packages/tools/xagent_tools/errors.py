"""استثناهای کنترلی ابزارها."""

from __future__ import annotations

from typing import Any


class EscalateCategory(Exception):
    """ابزار در حین اجرا تشخیص داده عملیات در دسته حساس‌تری قرار می‌گیرد (مثلاً ورود رمز = لاگین)."""

    def __init__(self, category: str, reason: str, args: dict[str, Any] | None = None):
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.extra_args = args or {}
