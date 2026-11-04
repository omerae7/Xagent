"""قرارداد مشترک ارائه‌دهندگان مدل (Provider-agnostic).

همه ارائه‌دهندگان پیام‌ها، ابزارها و مصرف را به این ساختار یکسان تبدیل می‌کنند تا هسته ایجنت
مستقل از شرکت سازنده مدل باشد.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from xagent_core.redact import scrub

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ImagePart:
    mime: str
    data_b64: str


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMMessage:
    role: Role
    content: str = ""
    images: list[ImagePart] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    # محتوای خام پاسخ ارائه‌دهنده (مثلاً بلاک‌های thinking در Claude) که باید بدون تغییر بازپخش شود
    raw: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "images": [{"mime": i.mime, "data_b64": i.data_b64} for i in self.images],
            "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "is_error": self.is_error,
            "raw": self.raw,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LLMMessage:
        return cls(
            role=d["role"],
            content=d.get("content", ""),
            images=[ImagePart(**i) for i in d.get("images", [])],
            tool_calls=[ToolCall(**t) for t in d.get("tool_calls", [])],
            tool_call_id=d.get("tool_call_id"),
            tool_name=d.get("tool_name"),
            is_error=d.get("is_error", False),
            raw=d.get("raw"),
        )


@dataclass
class Usage:
    input_uncached: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Provider-reported subcategories; subsets are not added to the total again.
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    reported: bool = True

    @property
    def input_total(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: str
    model: str
    latency_ms: int = 0
    raw: dict[str, Any] | None = None
    provider_request_id: str | None = None


@dataclass
class ChatRequest:
    model: str
    system_stable: str
    system_dynamic: str
    messages: list[LLMMessage]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float | None = None
    json_mode: bool = False
    effort: Literal["low", "medium", "high"] | None = None


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        # پیام خطای ارائه‌دهنده ممکن است کلید یا توکن را تکرار کند؛ پیش از هر ذخیره/نمایش پاک می‌شود
        super().__init__(scrub(message))
        self.retryable = retryable
        self.status = status


class Provider(Protocol):
    kind: str

    async def chat(self, req: ChatRequest) -> LLMResponse: ...

    async def embed(self, model: str, texts: list[str], dim: int) -> list[list[float]]: ...

    async def transcribe(self, model: str, audio: bytes, mime: str, language: str = "fa") -> str: ...

    async def speak(self, model: str, text: str, voice: str) -> tuple[bytes, str]: ...

    async def aclose(self) -> None: ...
