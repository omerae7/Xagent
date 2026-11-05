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
from xagent_core.redact import MASK, register_secret, scrub
from xagent_core.text import truncate

from ..base import Tool, ToolContext, ToolResult, b, i, obj, s
from ..errors import EscalateCategory
from ..sandbox import TakeoverActive

_pw: Any = None
_browser: Any = None
_lock = asyncio.Lock()

# فیلد حساس: مقدار آن هرگز خوانده/سریال نمی‌شود (رمز، OTP، توکن، کارت). مقدار ورودی فقط برای دکمه‌ها برچسب است.
_SENSITIVE_FN = r"""(el) => {
  const t = (el.getAttribute('type') || '').toLowerCase(), ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const hint = [el.getAttribute('name'), el.id, el.getAttribute('placeholder'), el.getAttribute('aria-label')]
    .join(' ').toLowerCase();
  return t === 'password' || /password|one-time-code|cc-number|cc-csc/.test(ac)
    || (/^(input|textarea)$/i.test(el.tagName) && /pass|pwd|secret|token|otp|cvv|api.?key|رمز|گذرواژه/.test(hint));
}"""
_LABEL_FN = r"""(el) => {
  const t = (el.getAttribute('type') || '').toLowerCase();
  const own = /^(input)$/i.test(el.tagName) ? (['button', 'submit', 'reset'].includes(t) ? el.value : '')
              : (/^(textarea|select)$/i.test(el.tagName) ? '' : el.innerText);
  return (own || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title')
          || el.getAttribute('name') || '').trim();
}"""


def _with_helpers(js: str) -> str:
    return (js.replace("__XA_INFO__", "__XA_INFO_FN__").replace("__XA_SENSITIVE__", f"({_SENSITIVE_FN})")
            .replace("__XA_LABEL__", f"({_LABEL_FN})"))


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
    const sensitive = __XA_SENSITIVE__(el);
    const label = __XA_LABEL__(el).slice(0, 80);
    out.push({id: n, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '', label, sensitive,
              href: el.getAttribute('href') || ''});
    if (n >= 150) break;
  }
  return out;
}
"""
_MARK_JS = _with_helpers(_MARK_JS)


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

# ── آمادگی صفحه: موفقیت ناوبری (URL) با آمادگی سند و محتوای مشاهده‌شده یکی نیست ──
READY_TIMEOUT_MS = 10000  # سقف انتظار رندر محتوا پس از ناوبری (صفحات JavaScript-محور)
READY_POLL_MS = 250
READY_STABLE_POLLS = 2  # محتوای یکسان در نمونه‌های پیاپی = رندر پایدار
CONTENT_GRACE_MS = 1500  # صفحه پویا (محتوای همیشه در حال تغییر) پس از این مدت محتوا ثبت می‌شود
EMPTY_RETRIES = 1  # بارگذاری مجدد محدود وقتی سند کامل شد ولی هیچ محتوایی رندر نشد
_page_errors: dict[int, list[str]] = {}  # id(page) → خطاهای JavaScript صفحه (pageerror) از آخرین ناوبری
_SNAPSHOT_JS = """
() => { /* xa:snapshot */
  const b = document.body, text = b ? (b.innerText || '').trim() : '';
  const box = (el, min) => { const r = el.getBoundingClientRect(); return r.width >= min && r.height >= min; };
  const all = s => b ? Array.from(document.querySelectorAll(s)) : [];
  return {ready: document.readyState, title: (document.title || '').trim(), chars: text.length, sample: text.slice(0, 300),
          interactive: all('a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link]')
                         .filter(e => box(e, 1)).length,
          visual: all('img,canvas,svg,video,picture,iframe,embed,object').filter(e => box(e, 48)).length};
}
"""
_CHALLENGE_TEXT = re.compile(
    r"verify (that )?you are (a )?human|are you a (human|robot)|not a robot|robot check|enter the characters you see|"
    r"unusual traffic|checking (your browser|if the site connection is secure)|just a moment\.\.\.|press (and|&) hold|"
    r"complete the security check|من ربات نیستم|ربات نیستید", re.I)
_CHALLENGE_URL = re.compile(r"captcha|/cdn-cgi/challenge|challenge-platform|/sorry/", re.I)
_DENIED_TEXT = re.compile(r"access denied|access to this page has been denied|request blocked|"
                          r"you don't have permission to access|403 forbidden|error 1020|دسترسی (غیرمجاز|ممنوع|مسدود)", re.I)
_PLACEHOLDER = re.compile(r"^\s*(loading|please wait|در حال بارگذاری|لطفا? ?صبر کنید)[.…\s]*$|enable javascript|"
                          r"javascript (is )?(required|disabled)|جاوااسکریپت را فعال", re.I)
_STATE_MSG = {
    "blank": "سند بارگذاری شد ولی هیچ عنوان، متن، عنصر قابل تعامل یا محتوای تصویری رندر نشد. ناوبری انجام شد اما "
             "بازدید/بررسی سایت تأیید نشده است. علت از این شواهد مشخص نیست؛ درباره مسدودسازی یا علت دیگر حدس نزن.",
    "loading": "صفحه فقط پیام بارگذاری/نیاز به JavaScript نشان داد و محتوای اصلی در مهلت رندر نشد؛ بررسی سایت تأیید نشده است.",
    "visual_only": "صفحه فقط عناصر تصویری (بدون متن یا عنصر قابل تعامل) دارد؛ محتوا از DOM مشاهده نشد. برای بررسی، "
                   "browser_screenshot با تحلیل بینایی لازم است.",
    "challenge": "صفحه چالش ضدربات/کپچا نمایش داده شد؛ محتوای سایت در دسترس نیست. کپچا را حل نکن و دور نزن؛ به کارفرما گزارش بده.",
    "access_denied": "سایت پاسخ «دسترسی ممنوع» داد؛ محتوای درخواستی در دسترس نیست.",
    "auth": "این صفحه ورود/احراز هویت است؛ کاربر وارد نشده و محتوای درخواستی (مثلاً گفتگوها) در دسترس نیست. "
            "هیچ محتوایی را حدس نزن؛ به کارفرما گزارش بده که ورود لازم است.",
}


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


# ── هویت مرورگر جدا برای هر کارمند (F05): کوکی، LocalStorage، SessionStorage و نشست ورود جدا ──
# کارفرما (و اجرای بدون کارمند) همان Context پایدار پروفایل Chrome را دارد؛ نشست‌های موجود دست نمی‌خورند.
_identities: dict[str, Any] = {}
_default_ctx: list[Any] = []  # [browser, Context پایدار کارفرما (پروفایل Chrome)] برای اتصال فعلی


def _identity_path(emp: str):  # noqa: ANN202
    import hashlib

    d = get_settings().data_dir / "browser-identities"
    return d / f"{hashlib.sha256(emp.encode()).hexdigest()[:24]}.state"


def _load_identity(emp: str) -> dict | None:
    import json as _json

    from xagent_core.security import decrypt_secret

    path = _identity_path(emp)
    try:
        plain = decrypt_secret(path.read_bytes()) if path.exists() else None
        return _json.loads(plain) if plain else None
    except (OSError, ValueError):
        return None  # حالت خراب/ناخوانا → هویت تازه (ورود دوباره لازم)، نه هویت دیگری


async def _save_identity(emp: str) -> None:
    """ذخیره رمزشده وضعیت هویت کارمند تا پس از ری‌استارت فرایند هم جدا و بازیابی‌پذیر بماند."""
    import json as _json

    from xagent_core.security import encrypt_secret

    bctx = _identities.get(emp)
    if not emp or bctx is None:
        return
    try:
        state = await bctx.storage_state()
        path = _identity_path(emp)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(encrypt_secret(_json.dumps(state)))
        tmp.chmod(0o600)
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — ذخیره ناموفق هرگز اثر کاربر را متوقف نمی‌کند
        pass


async def _shared_hosts(ctx: ToolContext | None, emp: str) -> set[str]:
    session = getattr(ctx, "session", None) if ctx is not None else None
    if session is None or not emp:
        return set()
    try:
        from xagent_core.settings_store import BrowserSharingSettings, get_setting

        st = await get_setting(session, "browser_sharing", BrowserSharingSettings)
        return {h.lower() for h in st.shares.get(emp, [])}
    except Exception:  # noqa: BLE001 — خطای خواندن سیاست = بدون اشتراک (پیش‌فرض امن)
        return set()


async def _browser_conn():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.connect_over_cdp(_cdp_url(), timeout=15000)
        if not _default_ctx or _default_ctx[0] is not _browser:
            # اتصال تازه: Contextهای اتصال قبلی معتبر نیستند (هویت‌ها از وضعیت ذخیره‌شده بازسازی می‌شوند).
            # Context پایدار پروفایل کارفرما همان لحظه اتصال، پیش از ساخت هویت کارمندان، ثبت می‌شود؛
            # ترتیب browser.contexts پس از ساخت Contextهای تازه تضمین‌شده نیست.
            _identities.clear()
            default = _browser.contexts[0] if _browser.contexts else await _browser.new_context()
            _default_ctx[:] = [_browser, default]
        return _browser


async def _context(ctx: ToolContext | None = None, host: str | None = None):
    """Context هویت این اجرا: کارفرما → Context پایدار؛ کارمند → Context جدای خودش؛ اشتراک فقط با سیاست صریح میزبان."""
    browser = await _browser_conn()
    emp = _employee_of(ctx)
    if not emp or (host and host.lower() in await _shared_hosts(ctx, emp)):
        return _default_ctx[1]
    async with _lock:
        bctx = _identities.get(emp)
        if bctx is None:
            state = _load_identity(emp)
            bctx = await browser.new_context(storage_state=state) if state else await browser.new_context()
            _identities[emp] = bctx
        return bctx


async def _all_contexts() -> list:
    browser = await _browser_conn()
    seen, out = set(), []
    for c in [*_default_ctx[1:], *(browser.contexts or []), *_identities.values()]:
        if id(c) not in seen:
            seen.add(id(c))
            out.append(c)
    return out


def _may_adopt(ctx: ToolContext | None, page) -> bool:  # noqa: ANN001
    """تب موجود بدون مالک فعال: کارفرما (اجرای غیرکارمند) تب‌های خودش را می‌تواند به امانت بگیرد؛ کارمند فقط تب
    همان کارمند را. تب در دست اجرای دیگر هرگز."""
    if page.is_closed() or _owner_of(page) is not None:
        return False
    tab_emp = _page_employee.get(id(page), "")
    return tab_emp == _employee_of(ctx)


async def _page(ctx: ToolContext | None = None, prefer_host: str | None = None, create: bool = True,
                host: str | None = None):
    """تب این اجرا. prefer_host: اگر تب آزاد و مجازی با همان سایت باز است، همان به امانت برداشته می‌شود.
    host: میزبان مقصد برای انتخاب هویت (Context)؛ تب در Context اشتباه رها و تب درست ساخته می‌شود."""
    key = _session_key(ctx)
    page = _sessions.get(key)
    if page is not None and not page.is_closed() and (host or prefer_host) and hasattr(page, "context"):
        want = await _context(ctx, host or prefer_host)
        if page.context is not want:
            await release_session(key)  # هویت مقصد فرق دارد (مثلاً اشتراک کارفرما)؛ تب هویت دیگر استفاده نمی‌شود
            page = None
    if page is None or page.is_closed():
        bctx = await _context(ctx, host or prefer_host)
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
    emp = _page_employee.get(id(page), "") if page is not None else ""
    if emp:
        await _save_identity(emp)
    if page is not None and not adopted:
        _page_employee.pop(id(page), None)
        _page_errors.pop(id(page), None)
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
# مشخصات واقعی عنصر هدف (و نزدیک‌ترین والد قابل تعامل) برای طبقه‌بندی اثر — مستقل از شیوه نشانی‌دهی
_INFO_FN = r"""(el0) => {
  const el = el0.closest('a,button,input,select,textarea,[role=button],[role=link],[onclick]') || el0;
  const f = el.closest('form');
  const attr = n => el.getAttribute(n) || '';
  const hint = [attr('id'), attr('name'), attr('class'), attr('data-action'), attr('data-testid'), attr('aria-label'),
                attr('title'), attr('formaction')].join(' ').slice(0, 200);
  const submits = f ? [...f.elements].filter(b => b.matches('button,input[type=submit]'))
    .map(b => __XA_LABEL__(b)).join(' ').slice(0, 160) : '';
  return {label: __XA_LABEL__(el).slice(0, 120), tag: el.tagName.toLowerCase(), type: attr('type').toLowerCase(),
          href: attr('href'), form: !!f, form_action: f ? (f.getAttribute('action') || '') : '', form_submits: submits,
          hint, sensitive: __XA_SENSITIVE__(el)};
}"""
_DESCRIBE_JS = """
(ref) => {
  const el = document.querySelector('[data-xa-id="' + ref + '"]');
  if (!el) return null;
  return (__XA_INFO__)(el);
}
"""


_INFO_FN = _with_helpers(_INFO_FN)
_DESCRIBE_JS = _with_helpers(_DESCRIBE_JS).replace("__XA_INFO_FN__", _INFO_FN)
_INFO_ALL_JS = f"els => els.slice(0, 25).map({_INFO_FN})"
_IS_SENSITIVE_JS = f"el => ({_SENSITIVE_FN})(el)"
_SEVERITY = {"payment": 3, "web_irreversible": 2, "external_message": 1}


def classify_effect(info: dict[str, Any] | None, *, submit: bool = False) -> str | None:
    """دسته Harness متناسب با اثر احتمالی اقدام؛ None یعنی اقدام عادی (باز کردن/خواندن/پیمایش)."""
    if not info:
        return None
    text = " ".join(str(info.get(k) or "") for k in ("label", "href", "hint"))
    if submit or info.get("type") == "submit":
        text += " " + " ".join(str(info.get(k) or "") for k in ("form_action", "form_submits"))
    for category, pat in _EFFECTS:
        if pat.search(text):
            return category
    if submit and not _SEARCH.search(text + " " + str(info.get("type") or "")):
        return "web_irreversible"  # ارسال فرم با اثر نامشخص: محافظه‌کارانه
    if not (info.get("label") or "").strip() and info.get("type") == "submit":
        return "web_irreversible"  # دکمه ارسال فرم بدون برچسب: اثر قابل تشخیص نیست
    return None


def _locator(page, a: dict[str, Any]):  # noqa: ANN001
    """عنصری که واقعاً عمل می‌کند: selector، یا متن/نام دسترس‌پذیر (دکمه‌های آیکونی با aria-label هم)."""
    if a.get("selector"):
        return page.locator(a["selector"])
    text = str(a.get("text") or "")
    return (page.get_by_text(text).or_(page.get_by_role("button", name=text)).or_(page.get_by_role("link", name=text)))


async def _describe_all(page, a: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ANN001
    if a.get("ref"):
        try:
            info = await page.evaluate(_DESCRIBE_JS, int(a["ref"]))
        except Exception:  # noqa: BLE001
            return []
        return [info] if isinstance(info, dict) else []
    try:
        infos = await _locator(page, a).evaluate_all(_INFO_ALL_JS)
    except Exception:  # noqa: BLE001 — هدف قابل تفکیک نیست؛ برچسب محافظه‌کارانه از خود رشته
        return [{"label": str(a.get("text") or a.get("selector") or ""), "tag": "", "type": "", "href": ""}]
    return [i for i in infos or [] if isinstance(i, dict)]


async def _describe(page, a: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ANN001
    """شدیدترین اثر میان همه عناصر منطبق (هدف مبهم با کنترل حساس → همان دسته حساس، نه فرض بی‌خطری)."""
    infos = await _describe_all(page, a)
    if not infos:
        return None
    return max(infos, key=lambda i: _SEVERITY.get(classify_effect(i) or "", 0))


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
        extra = (f" type={e['type']}" if e["type"] else "") + (" [حساس: مقدار خوانده نمی‌شود]" if e.get("sensitive") else "")
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


def _errors(page) -> list[str]:  # noqa: ANN001
    """خطاهای JavaScript صفحه (pageerror)؛ شنونده یک بار برای هر تب ثبت می‌شود."""
    errs = _page_errors.get(id(page))
    if errs is None:
        errs = _page_errors[id(page)] = []

        def on_error(e: Any) -> None:
            if len(errs) < 10:
                errs.append(truncate(str(e), 200))
        page.on("pageerror", on_error)
    return errs


async def _snapshot(page) -> dict[str, Any]:  # noqa: ANN001
    try:
        snap = await page.evaluate(_SNAPSHOT_JS)
    except Exception:  # noqa: BLE001 — سند در حال جابه‌جایی (ناوبری سمت کاربر)
        snap = None
    if not isinstance(snap, dict):
        return {"ready": "loading", "title": "", "chars": 0, "sample": "", "interactive": 0, "visual": 0}
    return snap


def _has_content(snap: dict[str, Any]) -> bool:
    if snap["chars"] < 300 and _PLACEHOLDER.search(snap.get("sample") or ""):
        return False
    return snap["chars"] > 0 or snap["interactive"] > 0


async def _settle(page) -> dict[str, Any]:  # noqa: ANN001
    """انتظار محدود برای آمادگی واقعی: readyState=complete و محتوای رندرشده پایدار، یا پایان مهلت (نه تأخیر ثابت)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline, last, stable, seen, snap = start + READY_TIMEOUT_MS / 1000, None, 0, None, {}
    for _ in range(READY_TIMEOUT_MS // READY_POLL_MS + 1):
        snap = await _snapshot(page)
        key = (snap["ready"], snap["title"], snap["chars"], snap["interactive"], snap["visual"])
        now = loop.time()
        if snap["ready"] == "complete" and _has_content(snap):
            seen = now if seen is None else seen
            stable = stable + 1 if key == last else 0
            if stable >= READY_STABLE_POLLS or (now - seen) * 1000 >= CONTENT_GRACE_MS:
                break
        else:
            seen = None
        last = key
        if now >= deadline:
            break
        await page.wait_for_timeout(READY_POLL_MS)
    snap["waited_ms"] = int((loop.time() - start) * 1000)
    return snap


def _classify(snap: dict[str, Any], status: int, url: str, text: str, auth: bool) -> str:
    """وضعیت قابل مشاهده صفحه؛ فقط «content» یعنی محتوای قابل استفاده واقعاً دیده شد."""
    short = text[:5000] if len(text) < 5000 else ""
    title = snap.get("title") or ""
    if _CHALLENGE_URL.search(urlparse(url or "").path) or _CHALLENGE_TEXT.search(f"{title}\n{short}"):
        return "challenge"
    if status == 401 or auth:
        return "auth"
    if status in (403, 451) or _DENIED_TEXT.search(f"{title}\n{short[:1500]}"):
        return "access_denied"
    if status >= 400:
        return "http_error"
    if not _has_content(snap):
        return "visual_only" if snap["visual"] and not snap["chars"] else (
            "loading" if snap["chars"] else "blank")
    return "content"


def _redirects(resp, final_url: str) -> list[str]:  # noqa: ANN001
    chain, req = [], getattr(getattr(resp, "request", None), "redirected_from", None)
    while req is not None and len(chain) < 10:
        chain.append(req.url)
        req = req.redirected_from
    chain.reverse()
    landed = getattr(resp, "url", "") or ""
    if landed and final_url and landed != final_url:  # هدایت سمت کاربر (JavaScript/meta) پس از پاسخ
        chain.append(landed)
    return chain


def _diagnostics(snap: dict[str, Any], status: int, errs: list[str], retries: int) -> str:
    line = (f"شواهد صفحه: readyState={snap['ready']}، HTTP {status or 'نامشخص'}، متن {snap['chars']} نویسه، "
            f"عناصر قابل تعامل {snap['interactive']}، عناصر تصویری {snap['visual']}، انتظار {snap['waited_ms']}ms"
            f"{f'، بارگذاری مجدد {retries}' if retries else ''}")
    return line + (f"\nخطاهای JavaScript صفحه: {' | '.join(errs[:3])}" if errs else "")


async def browser_tabs(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """کشف تب‌های نشست مرورگر مجاز و مالک هر تب (پیش از باز کردن صفحه تازه)."""
    await _guard()
    mine = await _context(ctx)
    me, emp = _session_key(ctx), _employee_of(ctx)
    lines, tabs = [], []
    pages = [(pg, c is mine) for c in await _all_contexts() for pg in c.pages]
    for idx, (pg, same_identity) in enumerate(pages):
        if pg.is_closed():
            continue
        owner = _owner_of(pg)
        tab_emp = _page_employee.get(id(pg), "")
        if not same_identity or (tab_emp != emp and owner != me):
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
    return ToolResult(True, "تب‌های مرورگر:\n" + out + "\nبرای استفاده از تب آزاد همان سایت، browser_open با همان نشانی را بزن."
                      "\n(فهرست تب‌ها فقط کشف است، نه مشاهده محتوای صفحه.)",
                      {"tabs": tabs, "content_observed": False}, untrusted=True)


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
    page = await _page(ctx, prefer_host=want if a.get("reuse", True) else None, host=want)
    before = page.url
    reused = _host(before) == want and before.rstrip("/").startswith(url.rstrip("/"))
    status, chain, retries = 0, [], 0
    errs = _errors(page)
    if not reused:
        errs.clear()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp else 0
            chain = _redirects(resp, page.url)
        except Exception as e:  # noqa: BLE001 — Timeout/خطای شبکه: گزارش دقیق، نه موفقیت
            state = "timeout" if "Timeout" in type(e).__name__ else "network"
            return ToolResult(False, f"باز کردن «{url}» ناموفق بود (ناوبری فقط تلاش شد؛ وضعیت: {state}): "
                              f"{type(e).__name__}: {truncate(str(e), 300)}\nنشانی فعلی تب: {page.url}",
                              {"url": page.url, "requested": url, "error": type(e).__name__, "page_state": state,
                               "navigation": "attempted", "content_observed": False})
    snap = await _settle(page)
    while (not _blank(page.url) and status < 400 and retries < EMPTY_RETRIES
           and _classify(snap, status, page.url, "", False) in ("blank", "loading")):
        retries += 1  # سند کامل ولی بی‌محتوا: یک بارگذاری مجدد محدود، نه فرض موفقیت
        try:
            resp = await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp else status
        except Exception:  # noqa: BLE001 — شواهد تلاش اول معتبر می‌ماند
            break
        snap = await _settle(page)
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    els = await _elements(page)
    title = await page.title()
    now_host = _host(page.url)
    auth = await _auth_wall(page, text)
    state = "network" if _blank(page.url) or not now_host else _classify(snap, status, page.url, text, auth)
    observed = state == "content"
    data = {"url_before": before, "url": page.url, "requested": url, "status": status, "title": title,
            "reused_tab": reused or ctx is not None and _session_key(ctx) in _adopted, "auth_required": auth,
            "host_match": now_host == want, "page_state": state, "content_observed": observed,
            "navigation": "confirmed" if observed else ("attempted" if state == "network" else "partial"),
            "ready_state": snap["ready"], "text_chars": snap["chars"], "interactive": snap["interactive"],
            "visual": snap["visual"], "page_errors": list(errs), "redirect_chain": chain,
            "waited_ms": snap["waited_ms"], "retries": retries}
    head = f"عنوان: {title}\nنشانی: {page.url}"
    diag = _diagnostics(snap, status, errs, retries)
    if state == "network":
        return ToolResult(False, f"{head}\nصفحه بارگذاری نشد (تب خالی). محتوای درخواستی مشاهده نشد.\n{diag}", data)
    if not observed:
        reason = _STATE_MSG.get(state) or f"پاسخ HTTP {status}؛ صفحه درخواستی در دسترس نیست."
        return ToolResult(False, f"ناوبری جزئی (وضعیت صفحه: {state})؛ محتوای قابل استفاده مشاهده نشد.\n{head}\n{reason}\n"
                          f"{diag}" + (f"\n\n--- متن ---\n{truncate(text, 1500)}" if text.strip() else ""),
                          data, untrusted=True)
    redirect = "" if now_host == want else f"\nتوجه: به میزبان دیگری هدایت شد ({now_host})."
    return ToolResult(
        True,
        f"{head}{redirect}{' (تب موجود همین سایت استفاده شد)' if data['reused_tab'] else ''}\n{diag}\n\n--- متن ---\n"
        f"{truncate(text, 6000)}\n\n--- عناصر قابل تعامل ---\n{els}",
        data, untrusted=True,
    )


async def browser_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page(ctx, create=False)
    if page is None or _blank(page.url):
        return ToolResult(False, f"هیچ صفحه‌ای در تب این اجرا باز نیست ({getattr(page, 'url', 'about:blank') or 'about:blank'}). "
                          "خواندن تب خالی، مشاهده سایت درخواستی نیست؛ ابتدا browser_tabs یا browser_open.",
                          {"url": getattr(page, "url", "about:blank"), "content_observed": False, "page_state": "blank"})
    snap = await _settle(page)
    if a.get("selector"):
        try:
            text = await page.inner_text(a["selector"], timeout=10000)
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"عنصر «{a['selector']}» در {page.url} یافت/خوانده نشد: {type(e).__name__}",
                              {"url": page.url})
    else:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    auth = await _auth_wall(page, text)
    state = _classify(snap, 0, page.url, text, auth)
    data = {"url": page.url, "title": await page.title(), "chars": len(text), "page_state": state,
            "ready_state": snap["ready"], "page_errors": list(_errors(page)),
            "content_observed": bool(text.strip()) and state not in ("challenge", "access_denied", "auth", "loading")}
    if auth:
        data["auth_required"] = True
        return ToolResult(False, f"نشانی: {page.url}\nاین صفحه ورود است؛ محتوای درخواستی در دسترس نیست (ورود لازم است).",
                          data, untrusted=True)
    if state in ("challenge", "access_denied", "loading"):
        return ToolResult(False, f"نشانی: {page.url}\n{_STATE_MSG[state]}\n{_diagnostics(snap, 0, data['page_errors'], 0)}",
                          data, untrusted=True)
    if not text.strip():
        hint = f"\n{_STATE_MSG[state]}" if state in _STATE_MSG else ""
        return ToolResult(False, f"نشانی: {page.url}\nصفحه هیچ متن قابل‌خواندنی ندارد.{hint}\n"
                          f"{_diagnostics(snap, 0, data['page_errors'], 0)}", data)
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
        info = await _describe(page, a)
        if info is None and not a.get("ref"):
            return ToolResult(False, f"عنصر هدف «{a.get('selector') or a.get('text')}» در صفحه یافت نشد.", {"url": page.url})
        _enforce_effect(ctx, page, info)  # پیش از کلیک، سمت سرور، بر اساس عنصر واقعی
        if a.get("ref"):
            await page.click(target, timeout=15000)
        else:
            await _locator(page, a).first.click(timeout=15000)  # همان عنصری که طبقه‌بندی شد
        await page.wait_for_timeout(1000)
        els = await _elements(page)
    return ToolResult(True, f"کلیک انجام شد. نشانی فعلی: {page.url}\n--- عناصر ---\n{els}", untrusted=True)


async def browser_type(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    async with _op_lock(ctx):
        page = await _page(ctx)
        target = _target(a)
        el = page.locator(target).first if a.get("ref") else _locator(page, a).first
        input_type = (await el.get_attribute("type") or "").lower()
        autocomplete = (await el.get_attribute("autocomplete") or "").lower()
        sensitive = bool(await el.evaluate(_IS_SENSITIVE_JS))
        if sensitive:
            register_secret(str(a.get("value") or ""), min_len=6)  # هر جا (خطا/لاگ/خروجی) تکرار شود پنهان می‌شود
        if (input_type == "password" or "password" in autocomplete) and "login" not in ctx.extra.get("approved", set()):
            raise EscalateCategory("login", f"ورود رمز عبور در {urlparse(page.url).hostname}", {"url": page.url})
        if a.get("submit"):
            info = await _describe(page, a) or {}
            info["type"] = info.get("type") or input_type
            _enforce_effect(ctx, page, info, submit=True)
        try:
            await el.fill(a["value"], timeout=15000)
            if a.get("submit"):
                await el.press("Enter")
                await page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001 — پیام خطا هرگز مقدار حساس را حمل نمی‌کند
            msg = scrub(str(e)).replace(str(a.get("value") or "\0"), MASK) if sensitive else scrub(str(e))
            return ToolResult(False, f"وارد کردن مقدار ناموفق بود: {type(e).__name__}: {truncate(msg, 400)}",
                              {"sensitive_input": sensitive})
    return ToolResult(True, f"مقدار{' حساس' if sensitive else ''} وارد شد{' و ارسال شد' if a.get('submit') else ''}. "
                      f"نشانی: {page.url}", {"sensitive_input": sensitive}, untrusted=True)


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
    res.data["content_observed"] = res.data.get("vision_analyzed") is True  # فقط تحلیل واقعی بینایی، نه صرف گرفتن تصویر
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
