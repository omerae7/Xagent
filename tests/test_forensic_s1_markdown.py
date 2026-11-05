"""ممیزی جرم‌شناختی — مرحله ۱، F02: تزریق اسکریپت از Markdown گفتگو (جزء واقعی پنل در Chromium واقعی).

جزء ChatMarkdown از src پنل با esbuild همان مخزن ساخته و از یک مبدأ محلی («مبدأ داشبورد») بارگذاری می‌شود.
هر payload در صورت اجرا window.__xss را تنظیم می‌کند؛ همه درخواست‌های بیرونی مسدودند.
"""

from __future__ import annotations

import re

import pytest
import web_harness
from browser_env import CHROME, start

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست"),
              pytest.mark.skipif(not web_harness.available(), reason="node_modules پنل نصب نیست (npm ci)")]

PAYLOADS = [
    'نشانی: https://e.x/"onmouseover="window.__xss=1',                       # تزریق نقل‌قول در ویژگی
    'https://e.x/"onfocus="window.__xss=2"tabindex="0"autofocus="',
    '<img src=x onerror="window.__xss=3">',                                  # HTML خام
    '<script>window.__xss=4</script>',
    '[کلیک](javascript:window.__xss=5) و javascript:window.__xss=6',          # طرح javascript
    'data:text/html,<script>window.__xss=7</script> و https://e.x/%22onmouseover=%22window.__xss=8',
    'https://e.x/&quot;onmouseover=&quot;window.__xss=9 و https://e.x/&#34;onclick=&#34;window.__xss=9',
    '```\n<script>window.__xss=10</script>\nhttps://e.x/"onmouseover="window.__xss=11\n```',  # بلوک کد
    '**مهم https://e.x/"onmouseover="window.__xss=12**',
    '- https://e.x/"onclick="window.__xss=13\n- مورد دوم',
    '**ناتمام `کد https://e.x/"<b onmouseover="window.__xss=14">',        # Markdown ناقص
    'سلام! پیوند عادی: https://example.com/fa/page?q=1&x=2. و **مهم** و `کد` تمام.',
    '۱. اول\n۲. دوم https://example.org/path',
]

ENTRY = """
import { createRoot } from "react-dom/client";
import { ChatMarkdown } from "@web/components/markdown";
const payloads = (window as any).__PAYLOADS as string[];
createRoot(document.getElementById("root")!).render(
  <>{payloads.map((p, i) => <div data-case={i} key={i}><ChatMarkdown text={p} /></div>)}</>);
"""


@pytest.fixture(scope="module")
def site():
    import shutil

    folder, _ = web_harness.build(ENTRY)
    e, stop = start(web_harness.routes_for(folder))
    yield e
    stop()
    shutil.rmtree(folder, ignore_errors=True)


async def render_and_attack(web: str, payloads: list[str]) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page()
        dialogs: list[str] = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        await page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(web) else r.abort())
        await page.add_init_script(web_harness.payload_script(payloads))
        await page.goto(web + "/")
        await page.wait_for_selector("[data-case]")
        await page.evaluate("""() => {
          window.addEventListener('click', e => e.preventDefault(), true);
          for (const el of document.querySelectorAll('#root *')) {
            for (const t of ['mouseover', 'mouseenter', 'focus', 'click'])
              el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true}));
            if (el.focus) el.focus();
          }
        }""")
        await page.wait_for_timeout(300)
        state = await page.evaluate("""() => ({
          xss: window.__xss ?? null,
          handlers: [...document.querySelectorAll('#root *')].flatMap(el =>
            [...el.attributes].filter(a => /^on/i.test(a.name)).map(a => el.tagName + '.' + a.name)),
          scripts: document.querySelectorAll('#root script, #root iframe, #root img, #root object').length,
          hrefs: [...document.querySelectorAll('#root a')].map(a => a.getAttribute('href')),
          cases: [...document.querySelectorAll('[data-case]')].map(d => d.innerHTML),
          text: [...document.querySelectorAll('[data-case]')].map(d => d.textContent),
        })""")
        state["dialogs"] = dialogs
        await browser.close()
        return state


async def test_F02_no_markdown_payload_executes_in_dashboard_origin(site):
    st = await render_and_attack(site.web, PAYLOADS)
    assert st["xss"] is None and not st["dialogs"], f"اسکریپت در مبدأ داشبورد اجرا شد: {st['xss']}"
    assert not st["handlers"], f"ویژگی رویداد تزریق شد: {st['handlers']}"
    assert st["scripts"] == 0, "عنصر اجرایی/HTML خام رندر شد"
    assert all(h and h.startswith(("http://", "https://")) for h in st["hrefs"]), st["hrefs"]


async def test_F02_safe_rendering_preserves_persian_links_code_and_lists(site):
    st = await render_and_attack(site.web, PAYLOADS)
    assert "https://example.com/fa/page?q=1&x=2" in st["hrefs"] and "https://example.org/path" in st["hrefs"]
    assert "<strong>مهم</strong>" in st["cases"][11] and "<code>کد</code>" in st["cases"][11]
    assert st["text"][11].endswith("تمام.") and "سلام!" in st["text"][11]
    assert "<ol>" in st["cases"][12] and st["cases"][12].count("<li>") == 2
    assert re.search(r'<pre\b[^>]*><code>', st["cases"][7])
    assert 'tabindex="0"' in st["cases"][7]  # keyboard access does not change HTML escaping
    assert "<script>window.__xss=10</script>" in st["text"][7]  # متن، نه عنصر
    assert "<ul>" in st["cases"][9] and st["cases"][9].count("<li>") == 2
    assert "<img" in st["text"][2] and "javascript:window.__xss=6" in st["text"][4]  # به‌صورت متن نمایش داده شد
