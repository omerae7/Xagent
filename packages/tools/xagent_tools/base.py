"""تعریف ابزار، کانتکست اجرا و نتیجه ابزار."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class ExecutionOutcome(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    ATTEMPTED = "ATTEMPTED"
    OBSERVED = "OBSERVED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)
    # اعتبارسنجی قطعی نتیجه (مثلاً فایل واقعاً ساخته شد / پیام شناسه گرفت)
    verified: bool | None = None
    # خروجی از منبع بیرونی (وب/فایل/پیام شخص ثالث) — داده است نه دستور
    untrusted: bool = False
    images: list[tuple[str, str]] = field(default_factory=list)  # (mime, base64)

    @property
    def outcome(self) -> ExecutionOutcome:
        """Derived from evidence, not a model/handler's claimed status.

        Keep constructor and old ok/verified fields compatible. These describe
        one operation; they do not imply completion of the user's whole objective.
        """
        if any(self.data.get(k) is True for k in ("outcome_uncertain", "uncertain", "replay_blocked")):
            return ExecutionOutcome.UNKNOWN_EFFECT
        if self.data.get("attempted") is False:
            return ExecutionOutcome.NOT_STARTED
        if not self.ok:
            return ExecutionOutcome.FAILED
        return ExecutionOutcome.VERIFIED if self.verified is True else ExecutionOutcome.OBSERVED


@dataclass
class ToolContext:
    session: AsyncSession
    run_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    timezone: str
    trusted_owner: bool
    sandbox: Any
    publish: Callable[[str, dict[str, Any]], Awaitable[None]]
    vision: Callable[[bytes, str], Awaitable[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    group: str
    category: str | None = None  # دسته Harness؛ None = بی‌خطر/فقط خواندنی
    action: str = "run"
    label: str = ""
    source: str = "builtin"
    # تعیین پویای دسته Harness بر اساس آرگومان‌ها (مثلاً GET بی‌خطر است)
    category_fn: Callable[[dict[str, Any]], str | None] | None = None

    def category_for(self, args: dict[str, Any]) -> str | None:
        if self.category_fn is not None:
            try:
                return self.category_fn(args)
            except Exception:
                return self.category
        return self.category

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def s(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def i(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **kw}


def b(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}
