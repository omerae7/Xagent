"""رجیستری ابزارها: ابزارهای داخلی + ابزارهای نصب‌شده (MCP/GitHub) با انتخاب گزینشی برای صرفه‌جویی توکن."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.models import InstalledTool
from xagent_core.text import normalize_fa

from . import installer, mcp_client
from .base import Tool, ToolContext, ToolResult, obj, s
from .builtin import computer, core, external, telegram, web, work


async def _tool_install(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    out = await installer.install(ctx.session, a["source"], a.get("ref"), a.get("name"), actor="agent")
    return ToolResult(out.ok, out.message + ("\n" + out.log[-1500:] if not out.ok else ""), verified=out.ok)


INSTALL_TOOL = Tool(
    "tool_install",
    "نصب ابزار جدید از GitHub، npm:<package>، pypi:<package> یا نشانی سرور MCP؛ نصب و تست در محیط ایزوله.",
    obj({"source": s("نشانی مخزن یا بسته"), "ref": s("شاخه/تگ/نسخه (اختیاری)"), "name": s("نام دلخواه")}, ["source"]),
    _tool_install,
    "system",
    category="install_tool",
    label="نصب ابزار",
)

BUILTIN: list[Tool] = [*core.TOOLS, *work.TOOLS, *web.TOOLS, *computer.TOOLS, *telegram.TOOLS, *external.TOOLS,
                       INSTALL_TOOL]
BUILTIN_BY_NAME = {t.name: t for t in BUILTIN}
ALWAYS_GROUPS = {"core", "memory"}
# Read-only internet search works through HTTP, independent of Chromium/XFCE.
# Always make these tools discoverable even for ambiguous follow-up prompts.
ALWAYS_WEB_READ_TOOLS = {"web_search", "web_fetch"}

GROUP_LABELS = {
    "core": "پایه",
    "memory": "حافظه",
    "episodes": "سابقه کارها",
    "files": "فایل‌ها",
    "system": "سیستم",
    "tasks": "وظایف",
    "routines": "روتین‌ها",
    "crm": "CRM",
    "web": "وب",
    "browser": "مرورگر",
    "desktop": "دسکتاپ",
    "telegram": "تلگرام",
    "installed": "ابزارهای نصب‌شده",
    "external": "اجراکننده بیرونی (Cursor Cloud Agents)",
}


def _mcp_tool(t: InstalledTool, spec: dict[str, Any]) -> Tool:
    tool_name = spec["name"]
    full = mcp_client.safe_name("mcp", t.name, tool_name)
    config = dict(t.config or {})

    async def handler(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
        ok, out = await mcp_client.call_tool(t.name, config, tool_name, a)
        return ToolResult(ok, out, untrusted=True, verified=ok if ok else None)

    schema = spec.get("input_schema") or {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return Tool(
        full,
        f"[{t.name}] {spec.get('description', '')}"[:1000],
        schema,
        handler,
        "installed",
        category=t.harness_category or "external_tool",
        label=f"{t.name}/{tool_name}",
        source=f"mcp:{t.name}",
    )


async def installed_tools(session: AsyncSession) -> list[Tool]:
    rows = (
        await session.execute(
            select(InstalledTool).where(InstalledTool.enabled.is_(True), InstalledTool.status == "ready")
        )
    ).scalars().all()
    out: list[Tool] = []
    for t in rows:
        if t.kind in ("mcp_stdio", "mcp_http"):
            out.extend(_mcp_tool(t, spec) for spec in (t.tool_schemas or []))
    return out


async def select_tools(session: AsyncSession, groups: list[str], query: str = "", limit_installed: int = 12) -> list[Tool]:
    """فقط ابزارهای لازم به مدل داده می‌شوند (کاهش توکن و خطا)."""
    wanted = set(groups) | ALWAYS_GROUPS
    if "browser" in wanted:
        wanted.add("web")
    if "external" in wanted:
        from .cursor_agents import readiness

        # ابزار واگذاری فقط وقتی به مدل داده می‌شود که کارفرما Cursor را فعال و تنظیم کرده باشد
        if (await readiness(session))[0] not in ("ready", "untested"):
            wanted.discard("external")
    chosen = [t for t in BUILTIN if t.group in wanted or t.name in ALWAYS_WEB_READ_TOOLS]
    inst = await installed_tools(session)
    if inst:
        q = normalize_fa(query)
        matched = [t for t in inst if t.source.split(":")[-1].lower() in q or t.name.lower() in q]
        if "installed" in wanted or len(inst) <= limit_installed:
            matched = matched or inst
        chosen.extend(matched[:limit_installed] if matched else [])
    seen: set[str] = set()
    out = []
    for t in chosen:
        if t.name not in seen:
            seen.add(t.name)
            out.append(t)
    return out


async def all_tools(session: AsyncSession) -> list[Tool]:
    return [*BUILTIN, *(await installed_tools(session))]
