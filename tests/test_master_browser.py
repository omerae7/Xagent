"""مأموریت جامع — مرحله ۳: مرورگر و Telegram Web با Chromium واقعی (CDP) و صفحات آزمایشی محلی.

B1–B8 + قفل دسکتاپ. هیچ حساب واقعی تلگرام استفاده نمی‌شود؛ «Telegram Web» یک صفحه محلی با ساختار مشابه است.
اگر Chromium در دسترس نباشد این آزمون‌ها Skip می‌شوند (و در گزارش «BLOCKED» ثبت می‌شوند، نه موفق).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

CHROME = os.environ.get("XAGENT_TEST_CHROME") or next(
    (p for p in ("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome", shutil.which("chromium"),
                 shutil.which("google-chrome")) if p and os.path.exists(p)), None)
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست")]

TG = """<!doctype html><html><head><title>Telegram Web</title></head><body>
<div class="chatlist"><div class="chat">علی: جلسه فردا ساعت ۱۰</div><div class="chat">سارا: گزارش ارسال شد</div></div>
<input placeholder="Search"></body></html>"""
LOGIN = """<!doctype html><html><head><title>Telegram Web</title></head><body>
<h1>Log in to Telegram by QR Code</h1><p>Scan this QR code with your phone</p></body></html>"""


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/slow"):
            time.sleep(4)
        body = {"/tg/": TG, "/tg-login/": LOGIN}.get(self.path.split("?")[0], "<html><body>ok page</body></html>")
        if self.path.startswith("/missing"):
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def env():
    web_port, cdp_port = _port(), _port()
    srv = ThreadingHTTPServer(("127.0.0.1", web_port), _H)
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
    yield SimpleNamespace(web=f"http://127.0.0.1:{web_port}", cdp=f"http://127.0.0.1:{cdp_port}")
    proc.terminate()
    srv.shutdown()
    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
async def comp(env, monkeypatch):
    from xagent_tools.builtin import computer

    monkeypatch.setattr(computer, "_cdp_url", lambda: env.cdp)
    async def no_guard():
        return None
    monkeypatch.setattr(computer, "_guard", no_guard)
    computer._browser = None
    computer._pw = None
    yield computer
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


def ctx(run=None, employee=None, vision=None, sandbox=None):
    return SimpleNamespace(run_id=run or uuid.uuid4(), extra={"employee_id": employee, "approved": set()},
                           vision=vision, sandbox=sandbox, trusted_owner=employee is None)


async def _owner_tab(comp, url: str):
    """تب موجود کارفرما (مثلاً Telegram Web لاگین‌شده) که هیچ اجرایی آن را باز نکرده است."""
    bctx = await comp._context()
    page = await bctx.new_page()
    await page.goto(url)
    return page


# ── B1: تب مجاز Telegram Web موجود کشف و استفاده می‌شود (بدون تب تازه، بدون بستن تب کارفرما) ──
async def test_B1_existing_authorized_tab_is_discovered_and_reused(comp, env):
    owner_tab = await _owner_tab(comp, f"{env.web}/tg/")
    bctx = await comp._context()
    n_before = len(bctx.pages)
    c = ctx()
    tabs = await comp.browser_tabs(c, {})
    assert any(t.get("url", "").endswith("/tg/") and t["access"] == "free" for t in tabs.data["tabs"]), tabs.output
    res = await comp.browser_open(c, {"url": f"{env.web}/tg/"})
    assert res.ok and res.data["reused_tab"] and "جلسه فردا" in res.output, res.output
    assert len(bctx.pages) == n_before, "تب تازه ساخته شد با اینکه تب مجاز موجود بود"
    await comp.release_session(c.run_id)
    assert not owner_tab.is_closed(), "پایان اجرا تب کارفرما را بست"
    await owner_tab.close()


# ── B2: about:blank هرگز مشاهده Telegram Web حساب نمی‌شود ──
async def test_B2_blank_tab_read_and_screenshot_fail(comp):
    c = ctx(vision=None)
    res = await comp.browser_read(c, {})
    assert not res.ok and "about:blank" in res.output
    shot = await comp.browser_screenshot(c, {})
    assert not shot.ok


# ── B3: Telegram Web باز نیست → ناوبری، تأیید مقصد و مشاهده محتوا ──
async def test_B3_navigation_is_verified(comp, env):
    c = ctx()
    res = await comp.browser_open(c, {"url": f"{env.web}/tg/", "reuse": False})
    assert res.ok and res.data["host_match"] and res.data["url"].endswith("/tg/")
    read = await comp.browser_read(c, {"expect": "جلسه فردا"})
    assert read.ok and read.data["url"].endswith("/tg/")
    miss = await comp.browser_read(c, {"expect": "پیامی که وجود ندارد"})
    assert not miss.ok and miss.data["expect_found"] is False
    notfound = await comp.browser_open(c, {"url": f"{env.web}/missing", "reuse": False})
    assert not notfound.ok and notfound.data["status"] == 404


# ── B4: هدایت به صفحه ورود → «ورود لازم است»، نه پیام ساختگی ──
async def test_B4_login_wall_is_reported(comp, env):
    c = ctx()
    res = await comp.browser_open(c, {"url": f"{env.web}/tg-login/", "reuse": False})
    assert not res.ok and res.data["auth_required"] is True and "ورود" in res.output
    read = await comp.browser_read(c, {})
    assert not read.ok and read.data.get("auth_required")


# ── B5: Timeout و خطای شبکه → خطای دقیق، نه موفقیت ──
async def test_B5_timeout_and_unreachable_are_errors(comp, env, monkeypatch):
    monkeypatch.setattr(comp, "NAV_TIMEOUT_MS", 1500)
    c = ctx()
    res = await comp.browser_open(c, {"url": f"{env.web}/slow", "reuse": False})
    assert not res.ok and "Timeout" in res.output, res.output
    dead = await comp.browser_open(c, {"url": f"http://127.0.0.1:{_port()}/", "reuse": False})
    assert not dead.ok


# ── B6: بدون تحلیل بینایی واقعی، «دیدن» محتوای تصویر ادعا نمی‌شود ──
async def test_B6_screenshot_without_vision_is_not_an_observation(comp, env):
    from xagent_tools.builtin.computer import VISION_UNAVAILABLE

    async def no_vision(png, q):
        return VISION_UNAVAILABLE
    c = ctx(vision=no_vision)
    await comp.browser_open(c, {"url": f"{env.web}/tg/", "reuse": False})
    shot = await comp.browser_screenshot(c, {})
    assert not shot.ok and shot.data["vision_analyzed"] is False and shot.images

    async def real_vision(png, q):
        assert png[:4] == b"\x89PNG"
        return "فهرست گفتگو با دو پیام"
    c.vision = real_vision
    ok = await comp.browser_screenshot(c, {})
    assert ok.ok and ok.data["vision_analyzed"] is True


# ── B7: کارمند A تب خصوصی کارمند B را نمی‌بیند و نمی‌گیرد ──
async def test_B7_employee_cannot_take_other_employees_tab(comp, env):
    b = ctx(employee="emp-b")
    await comp.browser_open(b, {"url": f"{env.web}/tg/", "reuse": False})
    b_page = comp._sessions[str(b.run_id)]  # تب B همچنان در دست اجرای B است
    a = ctx(employee="emp-a")
    tabs = await comp.browser_tabs(a, {})
    assert "جلسه فردا" not in tabs.output and any(t["access"] == "private" for t in tabs.data["tabs"])
    res = await comp.browser_open(a, {"url": f"{env.web}/tg/"})
    assert comp._sessions[str(a.run_id)] is not b_page, "کارمند A تب کارمند B را برداشت"
    await comp.release_session(a.run_id)
    assert not b_page.is_closed() and res.ok


# ── B8: دو اجرای هم‌زمان: تب جدا، و پایان یکی تب دیگری را نمی‌بندد ──
async def test_B8_concurrent_runs_are_isolated(comp, env):
    r1, r2 = ctx(), ctx()
    a, b = await asyncio.gather(comp.browser_open(r1, {"url": f"{env.web}/tg/", "reuse": False}),
                                comp.browser_open(r2, {"url": f"{env.web}/other", "reuse": False}))
    assert a.ok and b.ok
    p1, p2 = comp._sessions[str(r1.run_id)], comp._sessions[str(r2.run_id)]
    assert p1 is not p2 and p1.url.endswith("/tg/") and p2.url.endswith("/other")
    await comp.release_session(r1.run_id)
    assert p1.is_closed() and not p2.is_closed()
    assert (await comp.browser_read(r2, {})).ok


# ── ریسک ۱۴: کنترل دسکتاپ مشترک در هر لحظه فقط در اختیار یک اجرا ──
async def test_desktop_control_lease(app_client, comp):
    actions = []

    class Sandbox:
        async def desktop_action(self, action, **kw):
            actions.append(action)
            return {"ok": True}
    sb = Sandbox()
    r1, r2 = ctx(sandbox=sb), ctx(sandbox=sb)
    assert (await comp.desktop_input(r1, {"action": "click", "x": 1, "y": 1})).ok
    busy = await comp.desktop_input(r2, {"action": "type", "text": "x"})
    assert not busy.ok and busy.data["error"] == "desktop_busy"
    await comp.release_session(r1.run_id)
    assert (await comp.desktop_input(r2, {"action": "type", "text": "x"})).ok
    assert actions == ["click", "type"]
    await comp.release_session(r2.run_id)


# ── B2 در سطح Pipeline: ادعای «بررسی Telegram Web» با تب خالی، موفق ثبت نمی‌شود ──
async def test_B2_pipeline_blank_claim_does_not_succeed(app_client, configured_model, mock_llm, comp):
    from autonomy_helpers import body_text, called, tool_names

    from xagent_agent.pipeline import AgentRunner
    from xagent_core.db import session_scope
    from xagent_core.models import Run

    mark = "B2-PIPE"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if "browser_read" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        if not any(n == "browser_read" for n, _ in called(body)):
            return ("browser_read", {})
        return "Telegram Web را باز کردم و گفتگوها را بررسی کردم؛ همه چیز مرتب است."
    async with session_scope() as s:
        run = Run(trigger="chat", input=f"با مرورگر Telegram Web را بررسی کن و گفتگوها را ببین ({mark})",
                  status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("b2").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        run = await s.get(Run, rid)
    assert run.status != "succeeded", "ادعای بررسی Telegram Web با تب about:blank موفق ثبت شد"
