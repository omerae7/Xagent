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
# مالکیت تب: تب ساخته‌شده توسط اجرا پس از پایان بسته می‌شود؛ تب موجود (مثلاً Telegram Web لاگین‌شده کارفرما) که
# اجرا «به امانت» برداشته بسته نمی‌شود و فقط آزاد می‌شود. تب در دست اجرای دیگر یا کارمند دیگر هرگز برداشته نمی‌شود.
MAX_SESSIONS = 4
NAV_TIMEOUT_MS = 45000
IDLE_EVICT_SECONDS = 1800  # فقط تب اجرای رهاشده (بیکار طولانی) آزاد می‌شود، نه تب اجرای فعال دیگر
_sessions: dict[str, Any] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_session_used: dict[str, float] = {}
_adopted: set[str] = set()
_page_employee: dict[int, str] = {}  # id(page) → کارمند سازنده/دارنده (حریم تب کارمندان)
VISION_UNAVAILABLE = "(مدل بینایی تنظیم نشده است)"
_AUTH_PATH = re.compile(r"/(login|log-in|signin|sign-in|auth|sso|oauth)\b|#/login", re.I)
_AUTH_TEXT = re.compile(r"log in to telegram|log in by (qr|phone)|scan (this|the) qr|ورود به تلگرام|"
                        r"sign in to continue|please log ?in|برای ادامه وارد شوید", re.I)


class BrowserBusy(Exception):
    pass


def _session_key(ctx: ToolContext | None) -> str:
    rid = getattr(ctx, "run_id", None) if ctx is not None else None
    return str(rid) if rid else "default"


def _employee_of(ctx: ToolContext | None) -> str:
    return str(((ctx.extra if ctx is not None else {}) or {}).get("employee_id") or "")


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _owner_of(page) -> str | None:  # noqa: ANN001
    for k, pg in _sessions.items():
        if pg is page and not pg.is_closed():
            return k
    return None


async def _context():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.connect_over_cdp(_cdp_url(), timeout=15000)
        return _browser.contexts[0] if _browser.contexts else await _browser.new_context()


def _may_adopt(ctx: ToolContext | None, page) -> bool:  # noqa: ANN001
    """تب موجود بدون مالک فعال: کارفرما (اجرای غیرکارمند) تب‌های خودش را می‌تواند به امانت بگیرد؛ کارمند فقط تب
    همان کارمند را. تب در دست اجرای دیگر هرگز."""
    if page.is_closed() or _owner_of(page) is not None:
        return False
    tab_emp = _page_employee.get(id(page), "")
    return tab_emp == _employee_of(ctx)


async def _page(ctx: ToolContext | None = None, prefer_host: str | None = None, create: bool = True):
    """تب این اجرا. prefer_host: اگر تب آزاد و مجازی با همان سایت باز است، همان به امانت برداشته می‌شود."""
    key = _session_key(ctx)
    page = _sessions.get(key)
    if page is None or page.is_closed():
        bctx = await _context()
        async with _lock:
            if prefer_host:
                for cand in list(bctx.pages):
                    if _host(cand.url) == prefer_host and _may_adopt(ctx, cand):
                        _sessions[key] = cand
                        _adopted.add(key)
                        _session_used[key] = asyncio.get_running_loop().time()
                        return cand
            if not create:
                return None
            now = asyncio.get_running_loop().time()
            live = [k for k, pg in _sessions.items() if not pg.is_closed()]
            if len(live) >= MAX_SESSIONS:
                idle = [k for k in live if now - _session_used.get(k, now) > IDLE_EVICT_SECONDS
                        and not _session_locks.get(k, asyncio.Lock()).locked()]
                if not idle:
                    raise BrowserBusy(f"ظرفیت تب‌های مرورگر ({MAX_SESSIONS}) در دست اجراهای فعال است")
                await release_session(min(idle, key=lambda k: _session_used.get(k, 0)))
            page = await bctx.new_page()
            _sessions[key] = page
            _page_employee[id(page)] = _employee_of(ctx)
    _session_used[key] = asyncio.get_running_loop().time()
    return page


def _op_lock(ctx: ToolContext | None) -> asyncio.Lock:
    return _session_locks.setdefault(_session_key(ctx), asyncio.Lock())


async def release_session(key: Any) -> None:
    """پایان اجرا: تب ساخته‌شده توسط همین اجرا بسته و تب به‌امانت‌گرفته فقط آزاد می‌شود؛ قفل دسکتاپ آزاد می‌شود.
    هرگز تب یا قفل اجرای دیگری را لمس نمی‌کند."""
    k = str(key)
    page = _sessions.pop(k, None)
    adopted = k in _adopted
    _adopted.discard(k)
    _session_locks.pop(k, None)
    _session_used.pop(k, None)
    if page is not None and not adopted:
        _page_employee.pop(id(page), None)
        try:
            if not page.is_closed():
                await page.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        await bus.release_lease(DESKTOP_LEASE, k)
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


def _blank(url: str) -> bool:
    return not url or url.startswith(("about:", "chrome://newtab", "chrome-error://", "data:,"))


async def _auth_wall(page, text: str) -> bool:  # noqa: ANN001
    """صفحه ورود/احراز هویت (محافظه‌کارانه): نشانی ورود، متن ورود شناخته‌شده یا فیلد رمز در صفحه کوتاه."""
    if _AUTH_PATH.search(page.url or "") or _AUTH_TEXT.search(text[:4000]):
        return True
    try:
        has_pw = await page.evaluate("() => !!document.querySelector('input[type=password]')")
    except Exception:  # noqa: BLE001
        has_pw = False
    return bool(has_pw) and len(text) < 3000


async def browser_tabs(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """کشف تب‌های نشست مرورگر مجاز و مالک هر تب (پیش از باز کردن صفحه تازه)."""
    await _guard()
    bctx = await _context()
    me, emp = _session_key(ctx), _employee_of(ctx)
    lines, tabs = [], []
    for idx, pg in enumerate(bctx.pages):
        if pg.is_closed():
            continue
        owner = _owner_of(pg)
        tab_emp = _page_employee.get(id(pg), "")
        if tab_emp != emp and owner != me:
            lines.append(f"[{idx}] (تب خصوصی کارمند/حساب دیگر — قابل مشاهده نیست)")
            tabs.append({"index": idx, "access": "private"})
            continue
        state = "همین اجرا" if owner == me else ("در دست اجرای دیگر" if owner else "آزاد (قابل استفاده)")
        try:
            title = await pg.title()
        except Exception:  # noqa: BLE001
            title = ""
        lines.append(f"[{idx}] {pg.url} — {truncate(title, 80)} — {state}")
        tabs.append({"index": idx, "url": pg.url, "title": title, "access": "own" if owner == me else
                     ("busy" if owner else "free")})
    out = "\n".join(lines) or "هیچ تبی باز نیست."
    return ToolResult(True, "تب‌های مرورگر:\n" + out + "\nبرای استفاده از تب آزاد همان سایت، browser_open با همان نشانی را بزن.",
                      {"tabs": tabs}, untrusted=True)


async def browser_open(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    async with _op_lock(ctx):
        try:
            return await _browser_open(ctx, a)
        except BrowserBusy as e:
            return ToolResult(False, str(e), {"error": "busy"})


async def _browser_open(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    url = a["url"] if "://" in a["url"] else "https://" + a["url"]
    want = _host(url)
    page = await _page(ctx, prefer_host=want if a.get("reuse", True) else None)
    before = page.url
    reused = _host(before) == want and before.rstrip("/").startswith(url.rstrip("/"))
    status = 0
    if not reused:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp else 0
        except Exception as e:  # noqa: BLE001 — Timeout/خطای شبکه: گزارش دقیق، نه موفقیت
            return ToolResult(False, f"باز کردن «{url}» ناموفق بود: {type(e).__name__}: {truncate(str(e), 300)}\n"
                              f"نشانی فعلی تب: {page.url}", {"url": page.url, "requested": url, "error": type(e).__name__})
        await page.wait_for_timeout(800)
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    els = await _elements(page)
    title = await page.title()
    now_host = _host(page.url)
    auth = await _auth_wall(page, text)
    data = {"url_before": before, "url": page.url, "requested": url, "status": status, "title": title,
            "reused_tab": reused or ctx is not None and _session_key(ctx) in _adopted, "auth_required": auth,
            "host_match": now_host == want}
    head = f"عنوان: {title}\nنشانی: {page.url}"
    if _blank(page.url) or not now_host:
        return ToolResult(False, f"{head}\nصفحه بارگذاری نشد (تب خالی). محتوای درخواستی مشاهده نشد.", data)
    if status >= 400:
        return ToolResult(False, f"{head}\nپاسخ HTTP {status}؛ صفحه درخواستی در دسترس نیست.", data, untrusted=True)
    if auth:
        return ToolResult(False, f"{head}\nاین صفحه ورود/احراز هویت است؛ کاربر وارد نشده و محتوای درخواستی "
                          "(مثلاً گفتگوها) در دسترس نیست. هیچ محتوایی را حدس نزن؛ به کارفرما گزارش بده که ورود لازم است.",
                          data, untrusted=True)
    redirect = "" if now_host == want else f"\nتوجه: به میزبان دیگری هدایت شد ({now_host})."
    return ToolResult(
        True,
        f"{head}{redirect}{' (تب موجود همین سایت استفاده شد)' if data['reused_tab'] else ''}\n\n--- متن ---\n"
        f"{truncate(text, 6000)}\n\n--- عناصر قابل تعامل ---\n{els}",
        data, untrusted=True,
    )


async def browser_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page(ctx, create=False)
    if page is None or _blank(page.url):
        return ToolResult(False, f"هیچ صفحه‌ای در تب این اجرا باز نیست ({getattr(page, 'url', 'about:blank') or 'about:blank'}). "
                          "خواندن تب خالی، مشاهده سایت درخواستی نیست؛ ابتدا browser_tabs یا browser_open.",
                          {"url": getattr(page, "url", "about:blank")})
    if a.get("selector"):
        try:
            text = await page.inner_text(a["selector"], timeout=10000)
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"عنصر «{a['selector']}» در {page.url} یافت/خوانده نشد: {type(e).__name__}",
                              {"url": page.url})
    else:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    data = {"url": page.url, "title": await page.title(), "chars": len(text)}
    if await _auth_wall(page, text):
        data["auth_required"] = True
        return ToolResult(False, f"نشانی: {page.url}\nاین صفحه ورود است؛ محتوای درخواستی در دسترس نیست (ورود لازم است).",
                          data, untrusted=True)
    if not text.strip():
        return ToolResult(False, f"نشانی: {page.url}\nصفحه هیچ متن قابل‌خواندنی ندارد.", data)
    if a.get("expect") and str(a["expect"]) not in text:
        data["expect_found"] = False
        return ToolResult(False, f"نشانی: {page.url}\nمتن مورد انتظار «{a['expect']}» در صفحه مشاهده نشد.\n"
                          f"{truncate(text, 2000)}", data, untrusted=True)
    els = await _elements(page)
    return ToolResult(True, f"نشانی: {page.url}\n{truncate(text, 8000)}\n\n--- عناصر ---\n{els}", data, untrusted=True)


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


async def _visual(ctx: ToolContext, png: bytes, question: str, where: str) -> ToolResult:
    """گرفتن تصویر و تحلیل بینایی دو عمل جداست: بدون تحلیل واقعی، «مشاهده محتوا» ادعا نمی‌شود."""
    desc = await ctx.vision(png, question) if ctx.vision else VISION_UNAVAILABLE
    if not desc or desc.strip() == VISION_UNAVAILABLE:
        return ToolResult(False, f"تصویر {where} گرفته شد ولی تحلیل بینایی انجام نشد (مدل بینایی در دسترس نیست)؛ "
                          "محتوای تصویر بررسی نشده است و نباید ادعا شود.", {"captured": True, "vision_analyzed": False},
                          images=[("image/png", base64.b64encode(png).decode())])
    return ToolResult(True, f"تحلیل تصویر {where}: {desc}", {"captured": True, "vision_analyzed": True}, untrusted=True)


async def browser_screenshot(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page(ctx, create=False)
    if page is None or _blank(page.url):
        return ToolResult(False, "هیچ صفحه‌ای در تب این اجرا باز نیست؛ تصویر تب خالی مشاهده سایت نیست.",
                          {"url": getattr(page, "url", "about:blank")})
    png = await page.screenshot(full_page=False, type="png")
    res = await _visual(ctx, png, a.get("question") or "این صفحه مرورگر را دقیق توصیف کن.", "صفحه مرورگر")
    res.data["url"] = page.url
    return res


async def desktop_screenshot(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    png = await ctx.sandbox.screenshot()
    return await _visual(ctx, png, a.get("question") or "صفحه دسکتاپ را توصیف کن و مختصات عناصر مهم را بگو.", "دسکتاپ")


# ── قفل کنترل دسکتاپ: ماوس/صفحه‌کلید یک دسکتاپ مشترک در هر لحظه فقط در اختیار یک اجراست ──
DESKTOP_LEASE = "desktop:control"
DESKTOP_LEASE_MS = 120_000


async def acquire_desktop(ctx: ToolContext) -> bool:
    key = _session_key(ctx)
    if await bus.extend_lease(DESKTOP_LEASE, key, DESKTOP_LEASE_MS):
        return True
    return await bus.acquire_lease(DESKTOP_LEASE, DESKTOP_LEASE_MS, owner=key) is not None


async def desktop_input(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    if not await acquire_desktop(ctx):
        return ToolResult(False, "کنترل دسکتاپ مشترک در اختیار اجرای دیگری است؛ اقدام انجام نشد (برای جلوگیری از اثر "
                          "روی کار دیگر). بعداً دوباره تلاش کن یا از مرورگر ساختاریافته استفاده کن.", {"error": "desktop_busy"})
    action = a["action"]
    payload = {k: a[k] for k in ("x", "y", "text", "keys", "button", "amount") if k in a}
    res = await ctx.sandbox.desktop_action(action, **payload)
    return ToolResult(bool(res.get("ok")), f"اقدام دسکتاپ «{action}» {'انجام شد' if res.get('ok') else 'ناموفق بود'}", res)


TOOLS = [
    Tool("browser_tabs", "فهرست تب‌های باز نشست مرورگر مجاز (نشانی، عنوان، آزاد/در دست اجرای دیگر). پیش از باز کردن "
         "سایتی که ممکن است با حساب لاگین‌شده باز باشد (مثل Telegram Web) استفاده کن.",
         obj({}), browser_tabs, "browser", label="تب‌های مرورگر"),
    Tool("browser_open", "باز کردن نشانی در مرورگر کروم ایجنت و دریافت متن و عناصر قابل‌کلیک (با شماره ref). اگر تب "
         "آزاد همان سایت باز باشد همان استفاده می‌شود. صفحه ورود، تب خالی یا خطای HTTP موفق حساب نمی‌شود.",
         obj({"url": s("نشانی"), "reuse": b("استفاده از تب آزاد همان سایت (پیش‌فرض بله)")}, ["url"]), browser_open,
         "browser", category="desktop_control", label="باز کردن صفحه"),
    Tool("browser_read", "خواندن متن صفحه فعلی تب این اجرا یا یک selector و فهرست عناصر. expect: متنی که باید دیده شود.",
         obj({"selector": s("CSS selector اختیاری"), "expect": s("متن مورد انتظار (اختیاری)")}), browser_read, "browser",
         label="خواندن مرورگر"),
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
