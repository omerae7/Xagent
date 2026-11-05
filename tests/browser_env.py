"""محیط آزمون مرورگر واقعی: Chromium محلی با CDP و سرور HTTP صفحه‌های کنترل‌شده (بدون اینترنت و حساب واقعی)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

CHROME = os.environ.get("XAGENT_TEST_CHROME") or next(
    (p for p in ("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome", shutil.which("chromium"),
                 shutil.which("google-chrome")) if p and os.path.exists(p)), None)

Route = tuple[int, dict[str, str], str]  # (status, headers, body)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start(routes: dict[str, Route | Callable[[], Route]]) -> tuple[SimpleNamespace, Callable[[], None]]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def do_GET(self):  # noqa: N802
            r = routes.get(self.path.split("?")[0], (404, {}, "<html><body>not found</body></html>"))
            status, headers, body = r() if callable(r) else r
            self.send_response(status)
            if not any(k.lower() == "content-type" for k in headers):
                self.send_header("Content-Type", "text/html; charset=utf-8")
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body.encode())
            except BrokenPipeError:
                pass

    web_port, cdp_port = free_port(), free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", web_port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="xa-chrome-")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={cdp_port}", f"--user-data-dir={profile}",
                             "--no-sandbox", "--disable-gpu", "--no-first-run", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", cdp_port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    def stop() -> None:
        proc.terminate()
        srv.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
    return SimpleNamespace(web=f"http://127.0.0.1:{web_port}", cdp=f"http://127.0.0.1:{cdp_port}"), stop


async def reset_computer(computer) -> None:  # noqa: ANN001
    for k in list(computer._sessions):
        await computer.release_session(k)
    if computer._browser is not None:
        try:
            await computer._browser.close()
        except Exception:  # noqa: BLE001
            pass
    if computer._pw is not None:
        await computer._pw.stop()
    computer._browser = computer._pw = None
