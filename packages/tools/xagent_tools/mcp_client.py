"""اتصال به سرورهای MCP (stdio داخل محیط ایزوله یا HTTP)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from xagent_core.text import truncate

from .sandbox import get_sandbox


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """سازگاری با نسخه‌های ۱ و ۲ کتابخانه MCP (camelCase / snake_case)."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def flatten_error(e: BaseException) -> str:
    subs = getattr(e, "exceptions", None)
    if subs:
        return "؛ ".join(flatten_error(x) for x in subs)
    return f"{type(e).__name__}: {e}"


def safe_name(*parts: str) -> str:
    n = "__".join(re.sub(r"[^a-zA-Z0-9_-]", "_", p) for p in parts)
    return n[:64]


async def _with_session(name: str, config: dict[str, Any], fn, timeout: float = 120):
    from mcp import ClientSession

    sandbox = get_sandbox()

    async def _run():
        async with sandbox.mcp_streams(name, config) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except BaseExceptionGroup as eg:  # پیام خوانا به‌جای TaskGroup
        raise RuntimeError(flatten_error(eg)) from eg


async def list_tools(name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    async def _fn(session):
        res = await session.list_tools()
        out = []
        for t in res.tools:
            out.append(
                {
                    "name": t.name,
                    "description": (t.description or "")[:1000],
                    "input_schema": _attr(t, "input_schema", "inputSchema") or {"type": "object", "properties": {}},
                    "annotations": (t.annotations.model_dump(by_alias=True, exclude_none=True)
                                    if getattr(t, "annotations", None) is not None else {}),
                }
            )
        return out

    return await _with_session(name, config, _fn, timeout=90)


async def call_tool(name: str, config: dict[str, Any], tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    async def _fn(session):
        res = await session.call_tool(tool, args)
        parts = []
        for c in res.content or []:
            ctype = getattr(c, "type", "")
            if ctype == "text":
                parts.append(c.text)
            elif ctype == "image":
                parts.append(f"[تصویر {_attr(c, 'mime_type', 'mimeType', default='')}]")
            elif ctype == "resource":
                parts.append(str(getattr(c, "resource", ""))[:2000])
            else:
                parts.append(str(c)[:1000])
        structured = _attr(res, "structured_content", "structuredContent")
        if structured and not parts:
            parts.append(str(structured)[:8000])
        return (not bool(_attr(res, "is_error", "isError", default=False))), truncate("\n".join(parts), 12000)

    return await _with_session(name, config, _fn, timeout=float(config.get("timeout", 180)))
