#!/usr/bin/env python3
"""دیمن کنترلی کامپیوتر ایجنت (داخل کانتینر دسکتاپ).

فقط در شبکه داخلی داکر و با توکن مشترک در دسترس است. وظایف:
- /exec      اجرای فرمان در محیط ایزوله (بدون هیچ رمز/کلید سیستم)
- /screenshot  تصویر از دسکتاپ
- /input     کنترل ماوس و کیبورد (xdotool) — هنگام Takeover کارفرما با 423 رد می‌شود
- /takeover  پرچم تحویل کنترل به کارفرما
- /mcp/*     پل Streamable HTTP برای سرورهای MCP از نوع stdio که داخل همین کانتینر اجرا می‌شوند
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import signal
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web

TOKEN = os.environ.get("DESKTOP_TOKEN", "")
RUNNER_MODE = os.environ.get("RUNNER_MODE") == "1"
DISPLAY = os.environ.get("DISPLAY", ":1")
HOME = os.environ.get("HOME", "/home/agent")
WORKDIR = os.path.join(HOME, "work")
MAX_OUT = 200_000
REQUEST_TIMEOUT = float(os.environ.get("MCP_REQUEST_TIMEOUT", "300"))
STATE = {"takeover": False}
SAFE_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": HOME, "LANG": "C.UTF-8",
            "DISPLAY": DISPLAY, "TERM": "xterm-256color"}


@web.middleware
async def auth(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    if not TOKEN or request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def run(cmd: str, cwd: str | None = None, timeout: int = 120, env: dict[str, str] | None = None) -> dict[str, Any]:
    os.makedirs(WORKDIR, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", cmd, cwd=cwd or WORKDIR, env={**SAFE_ENV, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"code": proc.returncode, "stdout": out.decode(errors="replace")[-MAX_OUT:],
                "stderr": err.decode(errors="replace")[-MAX_OUT:], "timed_out": False}
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return {"code": -9, "stdout": "", "stderr": f"timeout after {timeout}s", "timed_out": True}


async def health(_: web.Request) -> web.Response:
    cdp = False
    version = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=2)) as client:
            async with client.get("http://127.0.0.1:9223/json/version") as response:
                data = await response.json()
                version = data.get("Browser")
                cdp = response.status == 200 and isinstance(version, str) and bool(data.get("Protocol-Version"))
    except (ClientError, OSError, TimeoutError, ValueError):
        pass
    return web.json_response({"ok": True, "display": DISPLAY, "chromium_cdp": cdp, "takeover": STATE["takeover"],
                              "browser_cdp": cdp, "browser_version": version,
                              "git": bool(shutil.which("git")), "node": bool(shutil.which("node")),
                              "uv": bool(shutil.which("uv")), "mcp_servers": list(MCP.keys())})


async def exec_(request: web.Request) -> web.Response:
    d = await request.json()
    timeout = min(int(d.get("timeout", 120)), 1800)
    return web.json_response(await run(d["cmd"], d.get("cwd"), timeout, d.get("env") or {}))


async def screenshot(_: web.Request) -> web.Response:
    proc = await asyncio.create_subprocess_exec("import", "-display", DISPLAY, "-window", "root", "png:-",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    if proc.returncode != 0:
        return web.json_response({"error": err.decode(errors="replace")}, status=500)
    return web.json_response({"png_b64": base64.b64encode(out).decode()})


async def input_(request: web.Request) -> web.Response:
    if STATE["takeover"]:
        return web.json_response({"error": "takeover active"}, status=423)
    d = await request.json()
    a = d.get("action")
    x, y = d.get("x"), d.get("y")
    cmds: list[list[str]] = []
    if a in ("click", "double_click", "right_click", "move") and x is not None and y is not None:
        cmds.append(["xdotool", "mousemove", str(int(x)), str(int(y))])
        if a == "click":
            cmds.append(["xdotool", "click", "1"])
        elif a == "double_click":
            cmds.append(["xdotool", "click", "--repeat", "2", "1"])
        elif a == "right_click":
            cmds.append(["xdotool", "click", "3"])
    elif a == "type":
        cmds.append(["xdotool", "type", "--delay", "25", "--", str(d.get("text", ""))])
    elif a == "key":
        cmds.append(["xdotool", "key", "--", str(d.get("keys", ""))])
    elif a == "scroll":
        amount = int(d.get("amount", 3))
        cmds.append(["xdotool", "click", "--repeat", str(abs(amount)), "5" if amount > 0 else "4"])
    else:
        return web.json_response({"error": "invalid action"}, status=400)
    for c in cmds:
        p = await asyncio.create_subprocess_exec(*c, env={**SAFE_ENV})
        await p.wait()
    return web.json_response({"ok": True, "action": a})


async def takeover(request: web.Request) -> web.Response:
    d = await request.json()
    STATE["takeover"] = bool(d.get("on"))
    return web.json_response({"takeover": STATE["takeover"]})


# ───────────── پل MCP (stdio ← HTTP) ─────────────
class StdioServer:
    """یک فرایند MCP مشترک برای همه کلاینت‌ها (کارمندان/اجراها) با جداسازی کامل پاسخ‌ها.

    شناسه JSON-RPC هر درخواست به شناسه داخلی یکتا نگاشت می‌شود و پاسخ فقط به همان درخواست‌کننده با
    شناسه اصلی خودش برمی‌گردد؛ دو کلاینت با شناسه یکسان هرگز پاسخ هم را نمی‌گیرند. پاسخ دیررس پس از
    Timeout/قطع اتصال دور ریخته می‌شود. پس از خروج سرور، دست‌دهی initialize دوباره پخش می‌شود."""

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.init_result: dict[str, Any] | None = None
        self.init_msg: dict[str, Any] | None = None
        self.lock = asyncio.Lock()
        self.init_lock = asyncio.Lock()
        self.reader: asyncio.Task | None = None
        self._notified = False
        self._seq = 0

    async def ensure(self) -> None:
        async with self.lock:
            if self.proc and self.proc.returncode is None:
                return
            cmd = [self.config["command"], *self.config.get("args", [])]
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.config.get("cwd") or WORKDIR, env={**SAFE_ENV, **(self.config.get("env") or {})},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=16 * 1024 * 1024,
            )
            self.init_result = None
            self._notified = False
            self.reader = asyncio.create_task(self._read(self.proc))

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict) or "id" not in msg or "method" in msg:
                continue  # اعلان یا درخواست سرور → کلاینت: به هیچ درخواست‌کننده‌ای نسبت داده نمی‌شود
            fut = self.pending.pop(str(msg["id"]), None)
            if fut and not fut.done():
                fut.set_result(msg)
        await proc.wait()
        for key, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("MCP server exited"))
            self.pending.pop(key, None)

    async def _write(self, msg: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _request(self, msg: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._seq += 1
        internal = f"xa-{self._seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[internal] = fut
        try:
            await self._write({**msg, "id": internal})
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(internal, None)  # Timeout یا قطع اتصال: نشتی نیست و پاسخ دیررس به کسی نمی‌رسد
        return {**resp, "id": msg["id"]}

    async def _handshake(self, timeout: float) -> None:
        """پس از ری‌استارت سرور، دست‌دهی کلاینت اولیه تکرار می‌شود تا درخواست‌های بعدی پذیرفته شوند."""
        async with self.init_lock:
            if self.init_result is not None or self.init_msg is None:
                return
            resp = await self._request(self.init_msg, timeout)
            if "result" in resp:
                self.init_result = resp["result"]
                await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self._notified = True

    async def send(self, msg: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
        timeout = timeout or REQUEST_TIMEOUT
        await self.ensure()
        method = msg.get("method")
        if method == "initialize":
            async with self.init_lock:  # دو کلاینت هم‌زمان: فقط یک initialize به سرور می‌رسد
                if self.init_result is None:
                    self.init_msg = {k: v for k, v in msg.items() if k != "id"} | {"id": 0}
                    resp = await self._request(msg, timeout)
                    if "result" in resp:
                        self.init_result = resp["result"]
                    return resp
            return {"jsonrpc": "2.0", "id": msg["id"], "result": self.init_result}
        if method == "notifications/initialized":
            if self._notified:
                return None
            await self._write(msg)
            self._notified = True
            return None
        if self.init_result is None and self.init_msg is not None:
            await self._handshake(timeout)
        if "id" in msg and method is not None:
            return await self._request(msg, timeout)
        await self._write(msg)  # اعلان کلاینت
        return None

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


MCP: dict[str, StdioServer] = {}


async def mcp_register(request: web.Request) -> web.Response:
    d = await request.json()
    name, config = d["name"], d["config"]
    cur = MCP.get(name)
    if cur is None or cur.config != config:
        if cur:
            await cur.stop()
        MCP[name] = StdioServer(name, config)
    return web.json_response({"ok": True})


async def mcp_post(request: web.Request) -> web.Response:
    srv = MCP.get(request.match_info["name"])
    if srv is None:
        return web.json_response({"error": "not registered"}, status=404)
    body = await request.json()
    msgs = body if isinstance(body, list) else [body]
    replies = []
    for m in msgs:
        try:
            r = await srv.send(m)
        except Exception as e:  # noqa: BLE001
            r = {"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32603, "message": str(e)}} if "id" in m else None
        if r is not None:
            replies.append(r)
    if not replies:
        return web.Response(status=202)
    return web.json_response(replies if isinstance(body, list) else replies[0])


async def mcp_other(_: web.Request) -> web.Response:
    return web.Response(status=405)


def app() -> web.Application:
    a = web.Application(middlewares=[auth], client_max_size=32 * 1024 * 1024)
    routes = [web.get("/health", health)]
    if RUNNER_MODE:
        routes.extend([
            web.post("/exec", exec_),
            web.post("/mcp/register", mcp_register),
            web.post("/mcp/{name}", mcp_post),
            web.get("/mcp/{name}", mcp_other),
            web.delete("/mcp/{name}", mcp_other),
        ])
    else:
        routes.extend([web.post("/screenshot", screenshot), web.post("/input", input_),
                       web.post("/takeover", takeover)])
    a.add_routes(routes)
    return a


if __name__ == "__main__":
    os.makedirs(WORKDIR, exist_ok=True)
    print("agentd listening on :7000 —", shlex.quote(DISPLAY), flush=True)
    web.run_app(app(), host="0.0.0.0", port=int(os.environ.get("AGENTD_PORT", "7000")), print=None)
