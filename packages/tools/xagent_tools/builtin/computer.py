"""ابزارهای مرورگر ساختاریافته (Playwright روی Chrome دسکتاپ از طریق CDP) و کنترل تصویری دسکتاپ.

مرورگر داخل دسکتاپ ایجنت با پروفایل پایدار اجرا می‌شود تا کارفرما بتواند همان را زنده ببیند
و در صورت نیاز کنترل را به دست بگیرد (Takeover). هنگام Takeover همه اقدامات خودکار متوقف می‌شوند.
"""

from __future__ import annotations

import asyncio
import base64
import re
import socket
from typing import Any
from urllib.parse import urlparse, urlunparse

from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.text import truncate

from ..base import Tool, ToolContext, ToolResult, b, i, obj, s
from ..errors import EscalateCategory
from ..sandbox import TakeoverActive

_pw: Any = None
_browser: Any = None
_lock = asyncio.Lock()

_MARK_JS = """
() => {
  const sel = 'a[href],button,input,textarea,select,[role=button],[role=link],[contenteditable=true],[onclick]';
  const out = [];
  let n = 0;
  document.querySelectorAll('[data-xa-id]').forEach(e => e.removeAttribute('data-xa-id'));
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1;
    el.setAttribute('data-xa-id', String(n));
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder')
                   || el.getAttribute('title') || el.getAttribute('name') || '').trim().slice(0, 80);
    out.push({id: n, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '', label,
              href: el.getAttribute('href') || ''});
    if (n >= 150) break;
  }
  return out;
}
"""


def _cdp_url() -> str:
    """Chrome فقط Host به شکل IP یا localhost را می‌پذیرد؛ نام سرویس داکر به IP تبدیل می‌شود."""
    url = get_settings().desktop_cdp_url
    p = urlparse(url)
    host = p.hostname or "localhost"
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        ip = host
    return urlunparse(p._replace(netloc=f"{ip}:{p.port or 9222}"))


async def _guard() -> None:
    if await bus.is_takeover():
        raise TakeoverActive("کارفرما کنترل دسکتاپ را در دست دارد؛ اقدام خودکار مرورگر متوقف شد.")


# ── مدیر نشست مرورگر: هر اجرا تب اختصاصی خودش را دارد ──
# Context مشترک (Cookie و نشست‌های لاگین‌شده کارفرما حفظ می‌شود) ولی تب‌ها جدا: کلیک/تایپ یک اجرا هرگز در تب
# اجرای دیگر انجام نمی‌شود. هر اجرا قفل عملیات خودش را دارد؛ اجراهای مختلف هم‌زمان روی تب‌های جدا کار می‌کنند.
MAX_SESSIONS = 4
_sessions: dict[str, Any] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_session_used: dict[str, float] = {}


def _session_key(ctx: ToolContext | None) -> str:
    rid = getattr(ctx, "run_id", None) if ctx is not None else None
    return str(rid) if rid else "default"


async def _context():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.connect_over_cdp(_cdp_url(), timeout=15000)
        return _browser.contexts[0] if _browser.contexts else await _browser.new_context()


async def _page(ctx: ToolContext | None = None):
    key = _session_key(ctx)
    page = _sessions.get(key)
    if page is None or page.is_closed():
        bctx = await _context()
        async with _lock:
            live = [k for k, pg in _sessions.items() if not pg.is_closed()]
            if len(live) >= MAX_SESSIONS:  # آزادسازی قدیمی‌ترین نشست بیکار
                oldest = min(live, key=lambda k: _session_used.get(k, 0))
                if not _session_locks.get(oldest, asyncio.Lock()).locked():
                    await release_session(oldest)
            page = await bctx.new_page()
            _sessions[key] = page
    _session_used[key] = asyncio.get_running_loop().time()
    return page


def _op_lock(ctx: ToolContext | None) -> asyncio.Lock:
    return _session_locks.setdefault(_session_key(ctx), asyncio.Lock())


async def release_session(key: Any) -> None:
    """بستن تب اجرا پس از پایان آن (آزادسازی منابع)؛ در خطا بی‌صدا."""
    page = _sessions.pop(str(key), None)
    _session_locks.pop(str(key), None)
    _session_used.pop(str(key), None)
    if page is not None:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:  # noqa: BLE001
            pass


# ── سیاست اثرمحور: اثر واقعی کلیک/ارسال فرم پیش از اجرا تعیین می‌شود ──
_EFFECTS: list[tuple[str, re.Pattern[str]]] = [
    ("payment", re.compile(r"\b(pay|payment|checkout|buy|purchase|place order|subscribe|donate)\b|پرداخت|خرید|تسویه|"
                           r"ثبت سفارش|سفارش نهایی|اشتراک", re.I)),
    ("web_irreversible", re.compile(r"\b(delete|remove|erase|destroy|confirm|submit order|close account|deactivate|"
                                    r"unsubscribe|transfer|cancel (subscription|order)|change (password|email))\b|"
                                    r"حذف|پاک ?کن|تأیید نهایی|تایید نهایی|غیرفعال|انتقال|لغو اشتراک|تغییر رمز", re.I)),
    ("external_message", re.compile(r"\b(send|post|publish|reply|tweet|share|comment|submit)\b|ارسال|بفرست|انتشار|"
                                    r"پست|پاسخ بده|نظر بده", re.I)),
]
_SEARCH = re.compile(r"search|query|\bq\b|جست|سرچ|find", re.I)
_DESCRIBE_JS = """
(ref) => {
  const el = document.querySelector('[data-xa-id="' + ref + '"]');
  if (!el) return null;
  const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title')
                 || el.getAttribute('placeholder') || el.getAttribute('name') || '').trim().slice(0, 120);
  return {label, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
          href: el.getAttribute('href') || '', form: !!el.closest('form')};
}
"""


def classify_effect(info: dict[str, Any] | None, *, submit: bool = False) -> str | None:
    """دسته Harness متناسب با اثر احتمالی اقدام؛ None یعنی اقدام عادی (باز کردن/خواندن/پیمایش)."""
    if not info:
        return None
    text = " ".join(str(info.get(k) or "") for k in ("label", "href"))
    for category, pat in _EFFECTS:
        if pat.search(text):
            return category
    if submit and not _SEARCH.search(text + " " + str(info.get("type") or "")):
        return "web_irreversible"  # ارسال فرم با اثر نامشخص: محافظه‌کارانه
    if not (info.get("label") or "").strip() and info.get("type") == "submit":
        return "web_irreversible"  # دکمه ارسال فرم بدون برچسب: اثر قابل تشخیص نیست
    return None


async def _describe(page, a: dict[str, Any]) -> dict[str, Any] | None:
    if a.get("ref"):
        try:
            info = await page.evaluate(_DESCRIBE_JS, int(a["ref"]))
        except Exception:  # noqa: BLE001
            return None
        return info if isinstance(info, dict) else None
    if a.get("text"):
        return {"label": str(a["text"]), "tag": "", "type": "", "href": ""}
    return {"label": str(a.get("selector") or ""), "tag": "", "type": "", "href": ""}


def _enforce_effect(ctx: ToolContext, page, info: dict[str, Any] | None, *, submit: bool = False) -> None:
    category = classify_effect(info, submit=submit)
    if category and category not in (ctx.extra.get("approved") or set()):
        host = urlparse(getattr(page, "url", "") or "").hostname or ""
        label = (info or {}).get("label") or "(بدون برچسب)"
        raise EscalateCategory(category, f"اقدام مرورگر با اثر حساس: «{label}» در {host}",
                               {"element": label, "url": host, "effect": category})


async def _elements(page) -> str:
    els = await page.evaluate(_MARK_JS)
    lines = []
    for e in els[:120]:
        extra = f" type={e['type']}" if e["type"] else ""
        href = f" → {e['href'][:60]}" if e["href"] and e["tag"] == "a" else ""
        lines.append(f"[{e['id']}] <{e['tag']}{extra}> {e['label']}{href}")
    return "\n".join(lines)


async def browser_open(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    async with _op_lock(ctx):
        return await _browser_open(ctx, a)


async def _browser_open(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    page = await _page(ctx)
    url = a["url"] if "://" in a["url"] else "https://" + a["url"]
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(800)
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    els = await _elements(page)
    status = resp.status if resp else 0
    return ToolResult(
        status < 400,
        f"عنوان: {await page.title()}\nنشانی: {page.url}\n\n--- متن ---\n{truncate(text, 6000)}\n\n--- عناصر قابل تعامل ---\n{els}",
        {"url": page.url, "status": status},
        untrusted=True,
    )


async def browser_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page(ctx)
    if a.get("selector"):
        text = await page.inner_text(a["selector"], timeout=10000)
    else:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    els = await _elements(page)
    return ToolResult(True, f"نشانی: {page.url}\n{truncate(text, 8000)}\n\n--- عناصر ---\n{els}", untrusted=True)


def _target(a: dict[str, Any]) -> str:
    if a.get("ref"):
        return f'[data-xa-id="{int(a["ref"])}"]'
    if a.get("selector"):
        return a["selector"]
    if a.get("text"):
        return f"text={a['text']}"
    raise ValueError("ref یا selector یا text لازم است")


async def browser_click(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    async with _op_lock(ctx):
        page = await _page(ctx)
        target = _target(a)
        _enforce_effect(ctx, page, await _describe(page, a))  # پیش از کلیک، سمت سرور
        await page.click(target, timeout=15000)
        await page.wait_for_timeout(1000)
        els = await _elements(page)
    return ToolResult(True, f"کلیک انجام شد. نشانی فعلی: {page.url}\n--- عناصر ---\n{els}", untrusted=True)


async def browser_type(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    async with _op_lock(ctx):
        page = await _page(ctx)
        target = _target(a)
        el = page.locator(target).first
        input_type = (await el.get_attribute("type") or "").lower()
        autocomplete = (await el.get_attribute("autocomplete") or "").lower()
        if (input_type == "password" or "password" in autocomplete) and "login" not in ctx.extra.get("approved", set()):
            raise EscalateCategory("login", f"ورود رمز عبور در {urlparse(page.url).hostname}", {"url": page.url})
        if a.get("submit"):
            info = await _describe(page, a) or {}
            info["type"] = info.get("type") or input_type
            _enforce_effect(ctx, page, info, submit=True)
        await el.fill(a["value"], timeout=15000)
        if a.get("submit"):
            await el.press("Enter")
            await page.wait_for_timeout(1500)
    return ToolResult(True, f"مقدار وارد شد{' و ارسال شد' if a.get('submit') else ''}. نشانی: {page.url}", untrusted=True)


async def browser_screenshot(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page(ctx)
    png = await page.screenshot(full_page=False, type="png")
    if ctx.vision:
        desc = await ctx.vision(png, a.get("question") or "این صفحه مرورگر را دقیق توصیف کن.")
        return ToolResult(True, f"تحلیل تصویر صفحه: {desc}", untrusted=True)
    return ToolResult(True, "اسکرین‌شات گرفته شد (مدل بینایی تنظیم نشده است).",
                      images=[("image/png", base64.b64encode(png).decode())])


async def desktop_screenshot(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    png = await ctx.sandbox.screenshot()
    if ctx.vision:
        desc = await ctx.vision(png, a.get("question") or "صفحه دسکتاپ را توصیف کن و مختصات عناصر مهم را بگو.")
        return ToolResult(True, f"تحلیل دسکتاپ: {desc}", untrusted=True)
    return ToolResult(True, "اسکرین‌شات گرفته شد (مدل بینایی تنظیم نشده است).",
                      images=[("image/png", base64.b64encode(png).decode())])


async def desktop_input(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    action = a["action"]
    payload = {k: a[k] for k in ("x", "y", "text", "keys", "button", "amount") if k in a}
    res = await ctx.sandbox.desktop_action(action, **payload)
    return ToolResult(bool(res.get("ok")), f"اقدام دسکتاپ «{action}» انجام شد", res)


TOOLS = [
    Tool("browser_open", "باز کردن نشانی در مرورگر کروم ایجنت و دریافت متن و عناصر قابل‌کلیک (با شماره ref).",
         obj({"url": s("نشانی")}, ["url"]), browser_open, "browser", category="desktop_control", label="باز کردن صفحه"),
    Tool("browser_read", "خواندن متن صفحه فعلی مرورگر یا یک selector و فهرست عناصر.",
         obj({"selector": s("CSS selector اختیاری")}), browser_read, "browser", label="خواندن مرورگر"),
    Tool("browser_click", "کلیک روی عنصر با ref (از فهرست عناصر)، selector یا متن.",
         obj({"ref": i("شماره عنصر"), "selector": s("selector"), "text": s("متن عنصر")}), browser_click, "browser",
         category="desktop_control", label="کلیک در مرورگر"),
    Tool("browser_type", "وارد کردن متن در فیلد (ref/selector) و ارسال اختیاری. ورود رمز عبور نیازمند مجوز لاگین است.",
         obj({"ref": i("شماره عنصر"), "selector": s("selector"), "value": s("متن"), "submit": b("فشردن Enter")},
             ["value"]), browser_type, "browser", category="desktop_control", label="تایپ در مرورگر"),
    Tool("browser_screenshot", "گرفتن تصویر از صفحه مرورگر و تحلیل آن با مدل بینایی.",
         obj({"question": s("چه چیزی بررسی شود")}), browser_screenshot, "browser", label="تصویر مرورگر"),
    Tool("desktop_screenshot", "گرفتن تصویر از کل دسکتاپ ایجنت و تحلیل آن (فقط اگر ابزار ساختاریافته‌ای نبود).",
         obj({"question": s("چه چیزی بررسی شود")}), desktop_screenshot, "desktop", label="تصویر دسکتاپ"),
    Tool("desktop_input", "کنترل تصویری دسکتاپ: click(x,y)، double_click، right_click، type(text)، key(keys مثل ctrl+l)، scroll(amount).",
         obj({"action": s("click|double_click|right_click|move|type|key|scroll"), "x": i("x"), "y": i("y"),
              "text": s("متن"), "keys": s("کلیدها"), "amount": i("مقدار اسکرول")}, ["action"]), desktop_input,
         "desktop", category="desktop_control", label="کنترل دسکتاپ"),
]
