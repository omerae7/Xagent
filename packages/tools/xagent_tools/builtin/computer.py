"""ابزارهای مرورگر ساختاریافته (Playwright روی Chrome دسکتاپ از طریق CDP) و کنترل تصویری دسکتاپ.

مرورگر داخل دسکتاپ ایجنت با پروفایل پایدار اجرا می‌شود تا کارفرما بتواند همان را زنده ببیند
و در صورت نیاز کنترل را به دست بگیرد (Takeover). هنگام Takeover همه اقدامات خودکار متوقف می‌شوند.
"""

from __future__ import annotations

import asyncio
import base64
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


async def _page():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.connect_over_cdp(_cdp_url(), timeout=15000)
        ctx = _browser.contexts[0] if _browser.contexts else await _browser.new_context()
        page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
        await page.bring_to_front()
        return page


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
    page = await _page()
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
    page = await _page()
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
    page = await _page()
    await page.click(_target(a), timeout=15000)
    await page.wait_for_timeout(1000)
    els = await _elements(page)
    return ToolResult(True, f"کلیک انجام شد. نشانی فعلی: {page.url}\n--- عناصر ---\n{els}", untrusted=True)


async def browser_type(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page()
    target = _target(a)
    el = page.locator(target).first
    input_type = (await el.get_attribute("type") or "").lower()
    autocomplete = (await el.get_attribute("autocomplete") or "").lower()
    if (input_type == "password" or "password" in autocomplete) and "login" not in ctx.extra.get("approved", set()):
        raise EscalateCategory("login", f"ورود رمز عبور در {urlparse(page.url).hostname}", {"url": page.url})
    await el.fill(a["value"], timeout=15000)
    if a.get("submit"):
        await el.press("Enter")
        await page.wait_for_timeout(1500)
    return ToolResult(True, f"مقدار وارد شد{' و ارسال شد' if a.get('submit') else ''}. نشانی: {page.url}", untrusted=True)


async def browser_screenshot(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    await _guard()
    page = await _page()
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
