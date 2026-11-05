"""نصب، تست، به‌روزرسانی، غیرفعال‌سازی و Rollback ابزار از GitHub و منابع بیرونی.

منابع پشتیبانی‌شده:
- مخزن Git (GitHub/GitLab/…): تشخیص خودکار سرور MCP (Node/Python) یا مانیفست `xagent-tool.json`
- `npm:<package>`: سرور MCP منتشرشده در npm
- `pypi:<package>`: سرور MCP منتشرشده در PyPI
- نشانی HTTP یک سرور MCP راه دور (Streamable HTTP)

همه عملیات نصب و تست داخل محیط ایزوله (کامپیوتر ایجنت) انجام می‌شود، نه در Worker.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit, notify
from xagent_core.models import InstalledTool, ToolVersion
from xagent_core.redact import open_secrets, scrub, seal_secrets
from xagent_memory.revisions import record_change, snapshot

from . import mcp_client
from .sandbox import get_sandbox

GIT_URL = re.compile(r"^https://[\w.-]+/[\w.-]+/[\w.-]+?(\.git)?/?$")
NAME_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass
class InstallOutcome:
    ok: bool
    message: str
    tool_id: uuid.UUID | None = None
    log: str = ""


def _derive_name(source: str) -> str:
    base = source.rstrip("/").split("/")[-1].removesuffix(".git")
    base = base.split(":")[-1].split("@")[0] or "tool"
    return NAME_RE.sub("-", base.lower()).strip("-")[:48] or "tool"


def parse_source(source: str) -> tuple[str, str]:
    s = source.strip()
    if s.startswith("npm:"):
        return "npm", s[4:]
    if s.startswith("pypi:"):
        return "pypi", s[5:]
    if s.startswith(("http://", "https://")) and (s.rstrip("/").endswith("/mcp") or "/mcp?" in s):
        return "mcp_http", s
    if GIT_URL.match(s):
        return "git", s
    if s.startswith("/"):
        return "local", s
    raise ValueError("منبع نامعتبر است. نمونه: https://github.com/owner/repo یا npm:package یا pypi:package")


async def _run(log: list[str], cmd: str, cwd: str | None = None, timeout: int = 600) -> bool:
    sb = get_sandbox()
    res = await sb.exec(cmd, cwd=cwd, timeout=timeout)
    log.append(f"$ {cmd}\n(exit {res.code})\n{res.stdout[-3000:]}\n{res.stderr[-2000:]}")
    return res.code == 0


async def _detect_and_install(path: str, log: list[str]) -> dict[str, Any]:
    """تشخیص نوع پروژه، نصب وابستگی‌ها و برگرداندن پیکربندی اجرا."""
    sb = get_sandbox()
    manifest_raw = await sb.exec(f"cat {shlex.quote(path)}/xagent-tool.json 2>/dev/null || true")
    if manifest_raw.stdout.strip().startswith("{"):
        m = json.loads(manifest_raw.stdout)
        if m.get("install"):
            if not await _run(log, m["install"], cwd=path, timeout=900):
                raise RuntimeError("اجرای فرمان نصب مانیفست ناموفق بود")
        return {
            "kind": m.get("kind", "mcp_stdio"),
            "config": {"transport": "stdio", "command": m["command"], "args": m.get("args", []), "cwd": path,
                       "env": m.get("env", {})},
            "description": m.get("description", ""),
            "test": m.get("test"),
            "harness_category": m.get("harness_category", "external_tool"),
            "manifest": m,
        }
    pkg = await sb.exec(f"cat {shlex.quote(path)}/package.json 2>/dev/null || true")
    if pkg.stdout.strip().startswith("{"):
        p = json.loads(pkg.stdout)
        deps = {**p.get("dependencies", {}), **p.get("devDependencies", {})}
        ok = await _run(log, "npm ci --no-audit --no-fund || npm install --no-audit --no-fund", cwd=path, timeout=900)
        if not ok:
            raise RuntimeError("نصب وابستگی‌های npm ناموفق بود")
        if p.get("scripts", {}).get("build"):
            await _run(log, "npm run build", cwd=path, timeout=900)
        bin_field = p.get("bin")
        entry = None
        if isinstance(bin_field, str):
            entry = bin_field
        elif isinstance(bin_field, dict) and bin_field:
            entry = next(iter(bin_field.values()))
        entry = entry or p.get("main") or "index.js"
        is_mcp = any("modelcontextprotocol" in d for d in deps)
        return {
            "kind": "mcp_stdio" if is_mcp else "cli",
            "config": {"transport": "stdio", "command": "node", "args": [f"{path}/{entry}"], "cwd": path, "env": {}},
            "description": p.get("description", ""),
            "test": None,
            "harness_category": "external_tool",
            "manifest": {"package": p.get("name"), "version": p.get("version")},
        }
    py = await sb.exec(f"cat {shlex.quote(path)}/pyproject.toml 2>/dev/null || true")
    if py.stdout.strip():
        import tomllib

        meta = tomllib.loads(py.stdout)
        project = meta.get("project", {})
        ok = await _run(log, "uv venv .venv && uv pip install --python .venv/bin/python -e .", cwd=path, timeout=900)
        if not ok:
            ok = await _run(log, "python3 -m venv .venv && .venv/bin/pip install -e .", cwd=path, timeout=900)
        if not ok:
            raise RuntimeError("نصب وابستگی‌های پایتون ناموفق بود")
        scripts = project.get("scripts", {})
        if scripts:
            cmd, args = f"{path}/.venv/bin/{next(iter(scripts))}", []
        else:
            cmd, args = f"{path}/.venv/bin/python", ["-m", project.get("name", "server").replace("-", "_")]
        deps = " ".join(project.get("dependencies", []))
        return {
            "kind": "mcp_stdio" if "mcp" in deps else "cli",
            "config": {"transport": "stdio", "command": cmd, "args": args, "cwd": path, "env": {}},
            "description": project.get("description", ""),
            "test": None,
            "harness_category": "external_tool",
            "manifest": {"package": project.get("name"), "version": project.get("version")},
        }
    raise RuntimeError("نوع پروژه قابل تشخیص نیست (xagent-tool.json، package.json یا pyproject.toml یافت نشد)")


async def install(
    session: AsyncSession,
    source: str,
    ref: str | None = None,
    name: str | None = None,
    actor: str = "owner",
    env: dict[str, str] | None = None,
) -> InstallOutcome:
    kind, target = parse_source(source)
    if kind == "local" and actor == "agent":
        return InstallOutcome(False, "نصب از مسیر محلی فقط توسط کارفرما مجاز است.")
    name = NAME_RE.sub("-", (name or _derive_name(target)).lower()).strip("-")
    tool = (await session.execute(select(InstalledTool).where(InstalledTool.name == name))).scalar_one_or_none()
    before = snapshot("tool", tool) if tool else None
    if tool is None:
        tool = InstalledTool(name=name, kind="mcp_stdio", source_url=source, ref=ref, status="installing", enabled=False)
        session.add(tool)
    else:
        tool.status = "installing"
    await session.flush()
    await bus.publish("tool.changed", {"id": str(tool.id), "status": "installing"})
    log: list[str] = []
    sb = get_sandbox()
    try:
        if kind == "mcp_http":
            info = {"kind": "mcp_http", "config": {"transport": "http", "url": target, "headers": {}},
                    "description": "سرور MCP راه دور", "test": None, "harness_category": "external_tool",
                    "manifest": {}}
            version, path = "remote", target
        elif kind in ("npm", "pypi"):
            version = (ref or "latest").replace("/", "_")
            path = sb.tool_path(name, version)
            await _run(log, f"mkdir -p {shlex.quote(path)}")
            if kind == "npm":
                spec = target + (f"@{ref}" if ref else "")
                if not await _run(log, f"npm install --prefix . --no-audit --no-fund {shlex.quote(spec)}", cwd=path):
                    raise RuntimeError("نصب بسته npm ناموفق بود")
                pkg_name = target if not target.startswith("@") else target
                binp = await sb.exec(f"node -e \"const p=require('./node_modules/{pkg_name}/package.json');"
                                     f"const b=p.bin;console.log(typeof b==='string'?b:Object.values(b||{{}})[0]||p.main||'index.js')\"",
                                     cwd=path)
                entry = binp.stdout.strip() or "index.js"
                cfg = {"transport": "stdio", "command": "node",
                       "args": [f"{path}/node_modules/{pkg_name}/{entry}"], "cwd": path, "env": {}}
            else:
                spec = target + (f"=={ref}" if ref else "")
                if not await _run(log, f"uv venv .venv && uv pip install --python .venv/bin/python {shlex.quote(spec)}", cwd=path):
                    raise RuntimeError("نصب بسته PyPI ناموفق بود")
                cfg = {"transport": "stdio", "command": f"{path}/.venv/bin/{target.split('[')[0]}", "args": [],
                       "cwd": path, "env": {}}
            info = {"kind": "mcp_stdio", "config": cfg, "description": f"بسته {kind}: {target}", "test": None,
                    "harness_category": "external_tool", "manifest": {"package": target}}
        else:
            tmp = sb.tool_path(name, f"tmp-{uuid.uuid4().hex[:8]}")
            branch = f"--branch {shlex.quote(ref)} " if ref else ""
            src = f"file://{target}" if kind == "local" else target
            if not await _run(log, f"git clone --depth 1 {branch}{shlex.quote(src)} {shlex.quote(tmp)}", timeout=300):
                raise RuntimeError("دریافت مخزن ناموفق بود")
            sha = (await sb.exec("git rev-parse HEAD", cwd=tmp)).stdout.strip()[:12] or uuid.uuid4().hex[:12]
            version = sha
            path = sb.tool_path(name, version)
            await _run(log, f"rm -rf {shlex.quote(path)} && mv {shlex.quote(tmp)} {shlex.quote(path)}")
            info = await _detect_and_install(path, log)
        if env:
            info["config"].setdefault("env", {}).update(env)

        # تست در محیط ایزوله
        test_status, test_out = "skipped", ""
        schemas: list[dict[str, Any]] = []
        if info.get("test"):
            ok = await _run(log, info["test"], cwd=path if kind != "mcp_http" else None, timeout=600)
            test_status = "passed" if ok else "failed"
            test_out = log[-1][-4000:]
            if not ok:
                raise RuntimeError("تست ابزار ناموفق بود")
        if info["kind"] in ("mcp_stdio", "mcp_http"):
            schemas = await mcp_client.list_tools(name, info["config"])
            test_status = "passed"
            test_out = f"اتصال MCP موفق؛ {len(schemas)} ابزار شناسایی شد: " + "، ".join(s["name"] for s in schemas[:20])
            log.append(test_out)

        # پیکربندی اجرایی کامل همین نسخه (اسرار رمزشده) برای Rollback دقیق؛ از پیکربندی نسخه دیگری ساخته نمی‌شود
        tv = ToolVersion(tool_id=tool.id, version=version, ref=ref, path=path,
                         manifest={**info.get("manifest", {}), "exec_config": seal_secrets(info["config"]),
                                   "kind": info["kind"]},
                         test_status=test_status, test_output=scrub(test_out))
        session.add(tv)
        await session.flush()
        tool.kind = info["kind"]
        tool.description = info.get("description") or tool.description
        tool.config = info["config"]
        tool.harness_category = info.get("harness_category", "external_tool")
        tool.tool_schemas = schemas
        tool.active_version_id = tv.id
        tool.status = "ready"
        tool.enabled = True
        tool.last_error = None
        tool.ref = ref
        tool.source_url = source
        await record_change(session, "tool", tool, "update" if before else "create", before,
                            f"نصب/به‌روزرسانی ابزار از {scrub(source)} (نسخه {version})", actor=actor, origin="panel")
        await audit(session, actor, "tool.install", name, {"source": scrub(source), "version": version})
        await notify(session, f"ابزار «{name}» نصب شد", scrub(test_out)[:300], level="success", link="/tools")
        await bus.publish("tool.changed", {"id": str(tool.id), "status": "ready"})
        return InstallOutcome(True, f"ابزار «{name}» با موفقیت نصب و تست شد (نسخه {version}).", tool.id, "\n".join(log))
    except Exception as e:  # noqa: BLE001
        msg = scrub(str(e))
        if before and before.get("active_version_id"):
            # بازگشت به نسخه پایدار قبلی
            tool.status = "ready"
            tool.enabled = bool(before.get("enabled"))
            tool.last_error = f"به‌روزرسانی ناموفق: {msg}"
        else:
            tool.status = "error"
            tool.enabled = False
            tool.last_error = msg
        await audit(session, actor, "tool.install_failed", name, {"source": scrub(source), "error": msg})
        await notify(session, f"نصب ابزار «{name}» ناموفق بود", msg[:300], level="error", link="/tools")
        await bus.publish("tool.changed", {"id": str(tool.id), "status": tool.status})
        return InstallOutcome(False, f"نصب ناموفق: {msg}", tool.id, "\n".join(log)[-8000:])


async def update(session: AsyncSession, tool_id: uuid.UUID, ref: str | None = None, actor: str = "owner") -> InstallOutcome:
    tool = await session.get(InstalledTool, tool_id)
    if tool is None or not tool.source_url:
        return InstallOutcome(False, "ابزار یافت نشد")
    return await install(session, tool.source_url, ref or tool.ref, tool.name, actor)


async def rollback(session: AsyncSession, tool_id: uuid.UUID, version_id: uuid.UUID | None = None,
                   actor: str = "owner") -> InstallOutcome:
    tool = await session.get(InstalledTool, tool_id)
    if tool is None:
        return InstallOutcome(False, "ابزار یافت نشد")
    versions = (
        await session.execute(
            select(ToolVersion).where(ToolVersion.tool_id == tool_id).order_by(ToolVersion.created_at.desc())
        )
    ).scalars().all()
    target = None
    if version_id:
        target = next((v for v in versions if v.id == version_id), None)
    else:
        idx = next((n for n, v in enumerate(versions) if v.id == tool.active_version_id), 0)
        candidates = [v for v in versions[idx + 1:] if v.test_status in ("passed", "skipped")]
        target = candidates[0] if candidates else None
    if target is None:
        return InstallOutcome(False, "نسخه قبلی سالمی برای بازگشت وجود ندارد")
    before = snapshot("tool", tool)
    stored = (target.manifest or {}).get("exec_config")
    legacy = not isinstance(stored, dict)
    kind = (target.manifest or {}).get("kind") or tool.kind
    if not legacy:
        cfg = open_secrets(stored)  # پیکربندی اجرایی دقیق همان نسخه
    else:
        # رکورد قدیمی بدون پیکربندی ذخیره‌شده: بازسازی تقریبی از پیکربندی فعلی؛ فقط با آزمون اتصال موفق پذیرفته می‌شود
        cfg = dict(tool.config or {})
        if cfg.get("cwd") and kind != "mcp_http":
            old_path = cfg["cwd"]
            cfg = json.loads(json.dumps(cfg).replace(old_path, target.path))
    schemas = tool.tool_schemas
    if kind in ("mcp_stdio", "mcp_http"):
        try:
            schemas = await mcp_client.list_tools(tool.name, cfg)
        except Exception as e:  # noqa: BLE001 — آزمون اتصال ناموفق: نسخه فعلی دست‌نخورده می‌ماند، «ready» ادعا نمی‌شود
            msg = scrub(str(e))
            await audit(session, actor, "tool.rollback_failed", tool.name, {"version": target.version, "error": msg[:300]})
            return InstallOutcome(False, f"Rollback به نسخه {target.version} انجام نشد: آزمون اتصال آن نسخه ناموفق بود "
                                  f"({msg[:200]}). نسخه فعلی تغییری نکرد.", tool.id)
    tool.config = cfg
    tool.kind = kind
    tool.tool_schemas = schemas
    tool.active_version_id = target.id
    tool.status = "ready"
    tool.enabled = True
    tool.last_error = None
    note = " (رکورد نسخه قدیمی/legacy: پیکربندی از نسخه فعلی بازسازی و با آزمون اتصال تأیید شد)" if legacy else ""
    await record_change(session, "tool", tool, "update", before, f"Rollback به نسخه {target.version}{note}", actor=actor,
                        origin="panel")
    await audit(session, actor, "tool.rollback", tool.name, {"version": target.version, "legacy_config": legacy})
    await bus.publish("tool.changed", {"id": str(tool.id), "status": "ready"})
    return InstallOutcome(True, f"ابزار «{tool.name}» به نسخه {target.version} بازگشت.{note}", tool.id)
