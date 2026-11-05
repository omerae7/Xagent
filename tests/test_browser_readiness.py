"""رگرسیون موفقیت کاذب مرورگر در تولید: «سایت Amazon باز کن» با عنوان/متن/عناصر خالی موفق ثبت شد.

Chromium واقعی (CDP) و صفحه‌های کنترل‌شده محلی؛ هیچ درخواستی به Amazon یا اینترنت نمی‌رود. صفحه «amazon-blank»
فقط همان خروجی مشاهده‌شده (سند بدون عنوان، متن و عنصر) را بازسازی می‌کند و درباره رفتار واقعی Amazon ادعایی ندارد.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from autonomy_helpers import body_text, called, tool_names
from browser_env import CHROME, reset_computer, start

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست")]

EMPTY = "<!doctype html><html><head></head><body></body></html>"
OK = """<!doctype html><html><head><title>فروشگاه نمونه</title></head><body>
<h1>پیشنهادهای امروز</h1><p>کتاب، لوازم خانه و الکترونیک</p><a href="/ok/deals">همه پیشنهادها</a>
<input placeholder="جست‌وجو"><button>جست‌وجو</button></body></html>"""
SPA_SLOW = """<!doctype html><html><head></head><body><div id="app"></div><script>
setTimeout(() => { document.title = "فروشگاه کند"; document.getElementById('app').innerHTML =
 '<h1>محصولات امروز</h1><button>افزودن به سبد</button>'; }, 1500);</script></body></html>"""
SPA_BROKEN = """<!doctype html><html><head></head><body><div id="app"></div><script>
throw new Error("hydration failed: chunk 7 missing");</script></body></html>"""
DENIED = "<!doctype html><html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>" \
         "<p>You don't have permission to access this server.</p></body></html>"
CHALLENGE = """<!doctype html><html><head><title>Robot Check</title></head><body>
<h4>Enter the characters you see below</h4><p>Sorry, we just need to make sure you're not a robot.</p>
<form action="/errors/validateCaptcha"><img src="/captcha.jpg" width="200" height="70"><input name="field-keywords">
<button type="submit">Continue shopping</button></form></body></html>"""
LOADING = "<!doctype html><html><head><title>Shop</title></head><body><div>Loading...</div></body></html>"
IMAGES_ONLY = """<!doctype html><html><head><title></title></head><body>
<canvas width="400" height="300" style="background:#336"></canvas></body></html>"""

ROUTES = {
    "/amazon-blank/": (200, {}, EMPTY),
    "/ok/": (200, {}, OK),
    "/spa-slow/": (200, {}, SPA_SLOW),
    "/spa-broken/": (200, {}, SPA_BROKEN),
    "/denied/": (403, {}, DENIED),
    "/challenge/": (200, {}, CHALLENGE),
    "/canvas/": (200, {}, IMAGES_ONLY),
    "/loading/": (200, {}, LOADING),
    "/go/": (302, {"Location": "/ok/"}, ""),
}


@pytest.fixture(scope="module")
def env():
    e, stop = start(ROUTES)
    yield e
    stop()


@pytest.fixture
async def comp(env, monkeypatch):
    from xagent_tools.builtin import computer

    monkeypatch.setattr(computer, "_cdp_url", lambda: env.cdp)

    async def no_guard():
        return None
    monkeypatch.setattr(computer, "_guard", no_guard)
    # مهلت آماده‌شدن کوتاه‌تر برای سرعت آزمون (پیش‌فرض تولید بزرگ‌تر است)؛ کد قدیمی این نام‌ها را ندارد
    monkeypatch.setattr(computer, "READY_TIMEOUT_MS", 4000, raising=False)
    computer._browser = computer._pw = None
    yield computer
    await reset_computer(computer)


def ctx():
    return SimpleNamespace(run_id=uuid.uuid4(), extra={"employee_id": None, "approved": set()}, vision=None,
                           sandbox=None, trusted_owner=True)


# ── بازتولید دقیق: سند بدون عنوان، متن و عنصر ← ناوبری «جزئی»، نه بررسی تأییدشده سایت ──
async def test_exact_production_blank_document_is_not_success(comp, env):
    res = await comp.browser_open(ctx(), {"url": f"{env.web}/amazon-blank/", "reuse": False})
    assert res.ok is False, "سند بدون عنوان/متن/عنصر به‌عنوان باز شدن موفق سایت ثبت شد"
    assert res.data.get("page_state") == "blank" and res.data.get("navigation") == "partial"
    assert res.data.get("content_observed") is False
    assert res.data.get("ready_state") == "complete"


async def test_normal_page_is_confirmed_with_observed_content(comp, env):
    res = await comp.browser_open(ctx(), {"url": f"{env.web}/ok/", "reuse": False})
    assert res.ok and res.data["page_state"] == "content" and res.data["navigation"] == "confirmed"
    assert res.data["content_observed"] is True and "پیشنهادهای امروز" in res.output


async def test_slow_javascript_page_waits_beyond_800ms(comp, env):
    """محتوا ۱٫۵ ثانیه پس از بارگذاری رندر می‌شود؛ انتظار ثابت ۸۰۰ میلی‌ثانیه آن را نمی‌بیند."""
    res = await comp.browser_open(ctx(), {"url": f"{env.web}/spa-slow/", "reuse": False})
    assert res.ok and res.data["content_observed"] is True, res.output
    assert "محصولات امروز" in res.output and res.data["title"] == "فروشگاه کند"


async def test_broken_javascript_page_reports_page_errors(comp, env):
    res = await comp.browser_open(ctx(), {"url": f"{env.web}/spa-broken/", "reuse": False})
    assert not res.ok and res.data["page_state"] == "blank"
    assert res.data["page_errors"] and "hydration failed" in res.data["page_errors"][0]


async def test_access_denied_and_challenge_are_distinguished(comp, env):
    denied = await comp.browser_open(ctx(), {"url": f"{env.web}/denied/", "reuse": False})
    assert not denied.ok and denied.data["page_state"] == "access_denied" and denied.data["status"] == 403
    challenge = await comp.browser_open(ctx(), {"url": f"{env.web}/challenge/", "reuse": False})
    assert not challenge.ok and challenge.data["page_state"] == "challenge", challenge.output
    assert challenge.data["content_observed"] is False


async def test_visual_only_page_is_partial_not_confirmed(comp, env):
    res = await comp.browser_open(ctx(), {"url": f"{env.web}/canvas/", "reuse": False})
    assert not res.ok and res.data["page_state"] == "visual_only" and res.data["navigation"] == "partial"


async def test_loading_placeholder_is_bounded_and_retried(comp, env):
    res = await comp.browser_open(ctx(), {"url": f"{env.web}/loading/", "reuse": False})
    assert not res.ok and res.data["page_state"] == "loading" and res.data["retries"] == 1
    assert res.data["waited_ms"] <= comp.READY_TIMEOUT_MS + 1500, "انتظار آمادگی محدود نیست"


async def test_network_failure_and_redirect(comp, env):
    from browser_env import free_port

    dead = await comp.browser_open(ctx(), {"url": f"http://127.0.0.1:{free_port()}/", "reuse": False})
    assert not dead.ok and dead.data["page_state"] == "network" and dead.data["navigation"] == "attempted"
    moved = await comp.browser_open(ctx(), {"url": f"{env.web}/go/", "reuse": False})
    assert moved.ok and moved.data["url"].endswith("/ok/") and moved.data["redirect_chain"]


async def test_browser_read_blank_is_not_content(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/amazon-blank/", "reuse": False})
    read = await comp.browser_read(c, {})
    assert not read.ok and read.data.get("content_observed") is False
    tabs = await comp.browser_tabs(c, {})
    assert tabs.data.get("content_observed") is False, "فهرست تب‌ها مشاهده محتوای صفحه نیست"


async def test_screenshot_without_vision_is_not_content(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/ok/", "reuse": False})
    shot = await comp.browser_screenshot(c, {})
    assert not shot.ok and shot.data["content_observed"] is False


async def test_failed_navigation_is_not_excused_by_tab_listing():
    from xagent_agent.validation import evidence_issues

    blank = {"call_id": "c1", "signature": "s1", "tool": "browser_open", "ok": False, "verified": None,
             "requires_verification": False, "content_observed": False}
    tabs = {"call_id": "c2", "signature": "s2", "tool": "browser_tabs", "ok": True, "verified": None,
            "requires_verification": False, "content_observed": False}
    assert evidence_issues({"evidence": [blank, tabs]}, True, "سایت Amazon باز کن")
    assert evidence_issues({"evidence": [tabs]}, True, "سایت فروشگاه را بررسی کن")
    assert not evidence_issues({"evidence": [tabs]}, True, "چه تب‌هایی در مرورگر باز است؟")
    assert not evidence_issues({"evidence": [tabs]}, True, "list open tabs")
    assert evidence_issues({"evidence": [tabs]}, True, "Amazon را در تب جدید باز کن")
    seen = {**blank, "call_id": "c3", "signature": "s3", "ok": True, "content_observed": True}
    assert not evidence_issues({"evidence": [blank, seen]}, True, "سایت Amazon باز کن")


# ── Pipeline: تکمیل باید هدف واقعی کاربر را بسنجد، نه تعداد ابزارهای موفق ──
async def _run(app_client, mock_llm, comp, mark: str, text: str, steps):
    from xagent_agent.pipeline import AgentRunner
    from xagent_core.db import session_scope
    from xagent_core.models import Run

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        return steps(body, [n for n, _ in called(body)])
    async with session_scope() as s:
        run = Run(trigger="chat", input=f"{text} ({mark})", status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("ready").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        return await s.get(Run, rid)


async def test_pipeline_open_site_on_blank_document_does_not_succeed(app_client, configured_model, mock_llm, comp, env):
    def steps(body, done):
        if "browser_open" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        if "browser_open" not in done:
            return ("browser_open", {"url": f"{env.web}/amazon-blank/", "reuse": False})
        return "سایت Amazon باز شد."
    run = await _run(app_client, mock_llm, comp, "AMZ-BLANK", "سایت Amazon باز کن", steps)
    assert run.status != "succeeded", "باز شدن سایت بدون هیچ محتوای مشاهده‌شده موفق ثبت شد"
    # به کاربر «ناوبری جزئی/بدون محتوای مشاهده‌شده» گزارش می‌شود، نه بررسی تأییدشده سایت
    assert run.status == "failed" and "محتوای قابل استفاده صفحه مشاهده نشد" in (run.error or "")
    opened = [e for e in run.state["evidence"] if e["tool"] == "browser_open"]
    assert opened and all(e["content_observed"] is False and "ناوبری جزئی" in e["summary"] for e in opened)


async def test_pipeline_tabs_only_is_not_inspection(app_client, configured_model, mock_llm, comp, env):
    def steps(body, done):
        if "browser_tabs" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        if "browser_tabs" not in done:
            return ("browser_tabs", {})
        return "سایت را باز کردم و بررسی کردم؛ همه چیز مرتب است."
    run = await _run(app_client, mock_llm, comp, "INSPECT-NAV", "سایت فروشگاه را با مرورگر باز کن و بررسی کن", steps)
    assert run.status != "succeeded", "فهرست تب‌ها به‌عنوان بررسی صفحه پذیرفته شد"


async def test_pipeline_open_real_content_succeeds(app_client, configured_model, mock_llm, comp, env):
    def steps(body, done):
        if "browser_open" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        if "browser_open" not in done:
            return ("browser_open", {"url": f"{env.web}/ok/", "reuse": False})
        return "فروشگاه باز شد؛ عنوان «پیشنهادهای امروز» دیده می‌شود."
    run = await _run(app_client, mock_llm, comp, "OPEN-OK", "سایت فروشگاه نمونه را باز کن", steps)
    assert run.status == "succeeded", run.error


async def test_pipeline_listing_tabs_when_asked_succeeds(app_client, configured_model, mock_llm, comp, env):
    def steps(body, done):
        if "browser_tabs" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        if "browser_tabs" not in done:
            return ("browser_tabs", {})
        return "فهرست تب‌های باز مرورگر بالا آمده است."
    run = await _run(app_client, mock_llm, comp, "TABS-ASK", "چه تب‌هایی در مرورگر باز است؟", steps)
    assert run.status == "succeeded", run.error
