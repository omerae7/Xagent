"""ممیزی جرم‌شناختی — مرحله ۶، F20: تفکیک «نشست نامعتبر» از «قطعی سرویس» در پنل (Chromium واقعی).

پنل واقعی با vite همان مخزن ساخته و با API شبیه‌ساز محلی سرو می‌شود؛ حالت API (عادی/401/503/قطع اتصال)
در حین آزمون تغییر می‌کند. هیچ سرویس واقعی یا حساب واقعی استفاده نمی‌شود.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import web_harness
from browser_env import CHROME, free_port

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست"),
              pytest.mark.skipif(not web_harness.available(), reason="node_modules پنل نصب نیست (npm ci)")]

STATE = {"mode": "ok"}
OK_JSON = {
    "/api/auth/me": {"id": "u1", "username": "admin", "role": "owner"},
    "/api/health": {"ok": True, "db": True, "redis": True, "worker": {"ready": True}},
    "/api/settings": {"settings": {"general": {"timezone": "Asia/Tehran", "calendar": "jalali", "agent_name": "X Agent"}}},
    "/api/dashboard": {},
}


def build_app(src_root: Path) -> str:
    out = tempfile.mkdtemp(prefix="xa-web-dist-")
    env = {**os.environ, "NODE_PATH": str(web_harness.WEB / "node_modules")}
    subprocess.run([str(web_harness.WEB / "node_modules/.bin/vite"), "build", "--outDir", out, "--emptyOutDir",
                    "--logLevel", "error"], cwd=src_root, env=env, check=True, capture_output=True, timeout=240)
    return out


def serve(dist: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def _json(self, code: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path.startswith("/api/"):
                mode = STATE["mode"]
                if path == "/api/events/stream":
                    self.send_response(204)
                    self.end_headers()
                    return
                if mode == "drop":  # خطای انتقال: اتصال بدون پاسخ بسته می‌شود
                    self.close_connection = True
                    self.connection.close()
                    return
                if mode == "401":
                    return self._json(401, {"detail": "ابتدا وارد شوید"})
                if mode == "503":
                    return self._json(503, {"detail": "service unavailable"})
                return self._json(200, OK_JSON.get(path, {"items": []}))
            f = Path(dist) / path.lstrip("/")
            if not f.is_file():
                f = Path(dist) / "index.html"
            data = f.read_bytes()
            ctype = {"js": "application/javascript", "css": "text/css", "html": "text/html; charset=utf-8",
                     "woff2": "font/woff2", "svg": "image/svg+xml"}.get(f.suffix.lstrip("."), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_POST = do_GET  # noqa: N815

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv.shutdown


@pytest.fixture(scope="module")
def app_url():
    dist = build_app(web_harness.WEB)
    url, stop = serve(dist)
    yield url
    stop()
    shutil.rmtree(dist, ignore_errors=True)


async def _page(pw):
    browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    return browser, await browser.new_page()


async def _until(fn, timeout: float) -> bool:
    for _ in range(int(timeout * 4)):
        if await fn():
            return True
        await asyncio.sleep(0.25)
    return False


async def run_flows(url: str) -> dict:
    """سه سناریو؛ خروجی برای مقایسه رفتار پیش و پس از اصلاح."""
    from playwright.async_api import async_playwright

    res: dict = {}
    async with async_playwright() as pw:
        browser, page = await _page(pw)
        pw_input = page.locator("input[type=password]")
        # ── الف) قطعی سرویس هنگام بارگذاری → صفحه «در دسترس نیست»، نه صفحه ورود؛ بازیابی خودکار ──
        STATE["mode"] = "503"
        await page.goto(url + "/")
        await asyncio.sleep(3)
        res["outage_load_login"] = await pw_input.count() > 0
        res["outage_load_screen"] = await page.locator("[data-testid=service-unavailable]").count() > 0
        STATE["mode"] = "ok"
        res["outage_load_recovered"] = await _until(lambda: page.locator("aside").count(), 15) and \
            await pw_input.count() == 0
        # ── ب) قطع انتقال در میانه نشست → خروج نه؛ نوار قطعی؛ سپس بازیابی ──
        if not res["outage_load_recovered"]:  # پنل باز نشده (مثلاً صفحه ورود ماند): برای ادامه سناریو دوباره بارگذاری
            await page.goto(url + "/")
            await _until(lambda: page.locator("aside").count(), 10)
        STATE["mode"] = "drop"
        links = page.locator("aside a[href^='/']")
        if await links.count():
            await links.nth(min(3, await links.count() - 1)).click()  # اقدام واقعی کاربر در پنل → درخواست API ناموفق
        else:
            await page.evaluate("window.dispatchEvent(new Event('focus'))")
        res["drop_banner"] = await _until(lambda: page.locator("[data-testid=outage-banner]").count(), 12)
        res["drop_login"] = await pw_input.count() > 0
        STATE["mode"] = "ok"
        res["drop_recovered"] = await _until(
            lambda: page.evaluate("!document.querySelector('[data-testid=outage-banner]')"), 15)
        # ── ج) 401 واقعی → صفحه ورود ──
        STATE["mode"] = "401"
        page2 = await browser.new_page()
        await page2.goto(url + "/")
        res["auth_login"] = await _until(lambda: page2.locator("input[type=password]").count(), 10)
        await browser.close()
    STATE["mode"] = "ok"
    return res


async def test_F20_outage_is_not_logout_and_401_is(app_url):
    r = await run_flows(app_url)
    assert not r["outage_load_login"] and r["outage_load_screen"], f"قطعی سرویس هنگام بارگذاری به صفحه ورود برد: {r}"
    assert r["outage_load_recovered"], f"پس از بازیابی سرویس، پنل خودکار باز نشد: {r}"
    assert r["drop_banner"] and not r["drop_login"], f"قطع انتقال در میانه نشست کاربر را خارج کرد: {r}"
    assert r["drop_recovered"], f"نوار قطعی پس از بازیابی برطرف نشد: {r}"
    assert r["auth_login"], f"401 واقعی به صفحه ورود نرسید: {r}"
