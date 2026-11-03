"""محیط ایزوله اجرای کد، ابزارهای نصب‌شده و کنترل دسکتاپ.

- `DesktopSandbox`: کامپیوتر اختصاصی ایجنت (کانتینر دسکتاپ بدون هیچ رمز/کلیدی). اجرای فرمان،
  نصب ابزار از GitHub و سرورهای MCP همه داخل همین کانتینر اجرا می‌شوند و Worker هرگز کد شخص ثالث
  را مستقیماً اجرا نمی‌کند. ارتباط از طریق دیمن کنترلی با توکن مشترک در شبکه داخلی.
- `LocalSandbox`: حالت سبک (بدون دسکتاپ) و تست — اجرای فرایند با محیط حداقلی (بدون متغیرهای محرمانه)،
  محدودیت منابع و پوشه کاری جدا.
"""

from __future__ import annotations

import asyncio
import base64
import os
import resource
import shlex
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from xagent_core import bus
from xagent_core.config import get_settings

SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")


@dataclass
class ExecResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class DesktopUnavailable(RuntimeError):
    pass


class TakeoverActive(RuntimeError):
    pass


def _limit_resources() -> None:  # pragma: no cover - در فرایند فرزند اجرا می‌شود
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
        os.setsid()
    except (ValueError, OSError):
        pass


class LocalSandbox:
    kind = "local"

    def __init__(self, root: Path | None = None):
        self.root = root or (get_settings().data_dir / "sandbox")
        self.root.mkdir(parents=True, exist_ok=True)

    def _env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
        env["HOME"] = str(self.root)
        if extra:
            env.update(extra)
        return env

    async def exec(
        self, cmd: str | list[str], cwd: str | None = None, timeout: int = 120, env: dict[str, str] | None = None
    ) -> ExecResult:
        args = cmd if isinstance(cmd, list) else ["bash", "-lc", cmd]
        workdir = Path(cwd) if cwd else self.root
        workdir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(workdir),
            env=self._env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limit_resources,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
        except TimeoutError:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            return ExecResult(-9, "", f"اجرا پس از {timeout} ثانیه متوقف شد", timed_out=True)

    def tool_path(self, name: str, version: str) -> str:
        return str(get_settings().tools_dir / name / version)

    @asynccontextmanager
    async def mcp_streams(self, name: str, config: dict[str, Any]):
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if config.get("transport") == "http":
            async with _http_streams(config["url"], config.get("headers")) as streams:
                yield streams
            return
        params = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env={k: str(v) for k, v in (config.get("env") or {}).items()} or None,
            cwd=config.get("cwd"),
        )
        async with stdio_client(params) as streams:
            yield streams

    async def screenshot(self) -> bytes:
        raise DesktopUnavailable("دسکتاپ در این نصب فعال نیست (حالت سبک).")

    async def desktop_action(self, action: str, **kw: Any) -> dict[str, Any]:
        raise DesktopUnavailable("دسکتاپ در این نصب فعال نیست (حالت سبک).")

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "kind": "local", "git": bool(shutil.which("git")), "node": bool(shutil.which("node"))}


@asynccontextmanager
async def _http_streams(url: str, headers: dict[str, str] | None = None):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers=headers or {}, timeout=httpx2.Timeout(60, read=300)) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            yield streams[0], streams[1]


class DesktopSandbox:
    kind = "desktop"

    def __init__(self) -> None:
        s = get_settings()
        self.base = s.desktop_url.rstrip("/")
        self.token = s.desktop_token
        self.runner_base = os.environ.get("XAGENT_RUNNER_URL", "http://runner:7000").rstrip("/")
        self.runner_token = os.environ.get("XAGENT_RUNNER_TOKEN", "")
        self._runner = httpx.AsyncClient(base_url=self.runner_base, headers={"Authorization": f"Bearer {self.runner_token}"}, timeout=httpx.Timeout(30, read=900))
        self._client = httpx.AsyncClient(
            base_url=self.base, headers={"Authorization": f"Bearer {self.token}"}, timeout=httpx.Timeout(30, read=900)
        )

    async def _post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        try:
            r = await self._client.post(path, json=payload, timeout=timeout)
        except httpx.HTTPError as e:
            raise DesktopUnavailable(f"کامپیوتر ایجنت در دسترس نیست: {e}") from e
        if r.status_code == 423:
            raise TakeoverActive("کارفرما کنترل دسکتاپ را در دست دارد؛ اقدام خودکار متوقف شد.")
        if r.status_code >= 400:
            raise DesktopUnavailable(f"خطای دیمن دسکتاپ HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    async def exec(
        self, cmd: str | list[str], cwd: str | None = None, timeout: int = 120, env: dict[str, str] | None = None
    ) -> ExecResult:
        command = cmd if isinstance(cmd, str) else shlex.join(cmd)
        response = await self._runner.post("/exec", json={
            "cmd": command, "cwd": cwd, "timeout": timeout, "env": env or {},
        }, timeout=timeout + 15)
        response.raise_for_status()
        d = response.json()
        return ExecResult(d.get("code", -1), d.get("stdout", ""), d.get("stderr", ""), d.get("timed_out", False))

    def tool_path(self, name: str, version: str) -> str:
        return f"/home/agent/tools/{name}/{version}"

    @asynccontextmanager
    async def mcp_streams(self, name: str, config: dict[str, Any]):
        if config.get("transport") == "http":
            async with _http_streams(config["url"], config.get("headers")) as streams:
                yield streams
            return
        # سرور stdio داخل کامپیوتر ایجنت اجرا و از طریق پل HTTP دیمن در دسترس است
        reg = await self._runner.post("/mcp/register", json={"name": name, "config": config})
        reg.raise_for_status()
        async with _http_streams(f"{self.runner_base}/mcp/{name}", {"Authorization": f"Bearer {self.runner_token}"}) as streams:
            yield streams

    async def screenshot(self) -> bytes:
        d = await self._post("/screenshot", {})
        return base64.b64decode(d["png_b64"])

    async def desktop_action(self, action: str, **kw: Any) -> dict[str, Any]:
        if await bus.is_takeover():
            raise TakeoverActive("کارفرما کنترل دسکتاپ را در دست دارد؛ اقدام خودکار متوقف شد.")
        return await self._post("/input", {"action": action, **kw})

    async def health(self) -> dict[str, Any]:
        try:
            r = await self._client.get("/health", timeout=5)
            return r.json() | {"ok": r.status_code == 200, "kind": "desktop"}
        except httpx.HTTPError as e:
            return {"ok": False, "kind": "desktop", "error": str(e)}


class RunnerSandbox(DesktopSandbox):
    """The same isolated tool runner, without any desktop or browser access."""
    kind = "runner"

    async def screenshot(self) -> bytes:
        raise DesktopUnavailable("دسکتاپ روی این نصب فعال نیست.")

    async def desktop_action(self, action: str, **kw: Any) -> dict[str, Any]:
        raise DesktopUnavailable("دسکتاپ روی این نصب فعال نیست.")

    async def health(self) -> dict[str, Any]:
        try:
            r = await self._runner.get("/health", timeout=5)
            return r.json() | {"ok": r.status_code == 200, "kind": "runner"}
        except httpx.HTTPError as e:
            return {"ok": False, "kind": "runner", "error": str(e)}


_sandbox: LocalSandbox | DesktopSandbox | RunnerSandbox | None = None


def get_sandbox() -> LocalSandbox | DesktopSandbox:
    global _sandbox
    if _sandbox is None:
        mode = os.environ.get("XAGENT_SANDBOX", "desktop")
        _sandbox = LocalSandbox() if mode == "local" else (RunnerSandbox() if mode == "runner" else DesktopSandbox())
    return _sandbox
