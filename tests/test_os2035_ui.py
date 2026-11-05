"""Built React app in real Chromium; API responses are synthetic, no live integrations."""

import os
import shutil
from pathlib import Path

import pytest
import web_harness
from browser_env import CHROME
from test_forensic_s6_outage_ui import OK_JSON, STATE, build_app, serve

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not CHROME or not web_harness.available(), reason="browser/web dependencies unavailable"
    ),
]


@pytest.fixture(scope="module")
def ui_url():
    original = dict(OK_JSON)
    OK_JSON.update(
        {
            "/api/dashboard": {"approvals_pending": 2},
            "/api/conversations/demo/messages": {
                "conversation": {"id": "demo", "title": "فضای عملیات"},
                "runs": {},
                "items": [
                    {
                        "id": "u",
                        "role": "user",
                        "content": "فرم را بررسی کن، بدون ارسال.",
                        "created_at": "2026-11-05T10:00:00Z",
                    },
                    {
                        "id": "a",
                        "role": "assistant",
                        "content": "مقدار فیلد مشاهده شد. هیچ پیامی ارسال نشد.\n\n```python\n"
                        + ("long_identifier_" * 20)
                        + "\n```",
                        "created_at": "2026-11-05T10:00:02Z",
                    },
                ],
            },
            "/api/conversations": {
                "items": [{"id": "demo", "title": "فضای عملیات", "updated_at": "2026-11-05T10:00:00Z"}]
            },
            "/api/settings/ai/models/available": {
                "items": [
                    {
                        "id": "synthetic",
                        "label": "مدل آزمایشی",
                        "provider": "local",
                        "capabilities": {"tools": True},
                    }
                ]
            },
            "/api/desktop/status": {"takeover": False, "health": {"ok": True, "browser_cdp": False}},
        }
    )
    STATE["mode"] = "ok"
    folder = build_app(web_harness.WEB)
    url, stop = serve(folder)
    yield url
    stop()
    shutil.rmtree(folder)
    OK_JSON.clear()
    OK_JSON.update(original)


@pytest.mark.parametrize("width", [320, 360, 375, 390, 430, 768, 1024, 1440, 1920])
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_chat_ownership_overflow_keyboard_and_reduced_motion(ui_url, width, theme):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        await page.add_init_script(f"localStorage.setItem('xa-theme', '{theme}')")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(ui_url + "/chat/demo")
        await page.get_by_text("فرم را بررسی کن، بدون ارسال.", exact=True).wait_for()
        await page.wait_for_timeout(150)
        user = await page.locator("[data-owner=user] .message-surface").bounding_box()
        agent = await page.locator("[data-owner=agent] .message-surface").bounding_box()
        assert user["x"] + user["width"] / 2 > agent["x"] + agent["width"] / 2, (width, user, agent)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        text = page.locator("textarea").first
        await text.fill("پیش‌نویس محلی")
        await page.set_viewport_size({"width": width, "height": 440})
        await page.wait_for_function(
            "document.documentElement.style.getPropertyValue('--visual-height') === '440px'"
        )
        await text.focus()
        await expect(text).to_be_in_viewport(ratio=1)
        box = await text.bounding_box()
        assert box["y"] + box["height"] <= 440, box
        assert not errors, errors
        artifact = os.environ.get("XAGENT_UI_ARTIFACT_DIR")
        if artifact and width in (360, 1440):
            await page.set_viewport_size({"width": width, "height": 900})
            await page.screenshot(path=str(Path(artifact) / f"chat-{theme}-{width}.png"), full_page=True)
        await browser.close()


async def test_all_navigation_surfaces_render_without_runtime_exceptions(ui_url):
    from playwright.async_api import async_playwright

    paths = [
        "/",
        "/tasks",
        "/routines",
        "/crm",
        "/desk",
        "/control",
        "/tools",
        "/memory",
        "/dream",
        "/desktop",
        "/usage",
        "/settings",
    ]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        for width in (360, 1440):
            await page.set_viewport_size({"width": width, "height": 1000})
            for path in paths:
                await page.goto(ui_url + path)
                await page.locator("main").wait_for()
                await page.wait_for_timeout(180)
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    width,
                    path,
                )
                assert not errors, (path, errors)
        await page.goto(ui_url + "/desktop")
        await page.get_by_text("مرورگر تأیید نشده", exact=True).wait_for()
        await browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_chat_wcag_a_and_aa_with_axe(ui_url, theme):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 390, "height": 900}, reduced_motion="reduce")
        await page.add_init_script(f"localStorage.setItem('xa-theme', '{theme}')")
        await page.goto(ui_url + "/chat/demo")
        await page.locator("[data-owner=user]").wait_for()
        await page.add_script_tag(path=str(web_harness.WEB / "node_modules/axe-core/axe.min.js"))
        results = await page.evaluate("""async () => {
            const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}});
            return {violations:r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)})),passes:r.passes.length};
        }""")
        assert results["violations"] == [], results
        assert results["passes"] > 0
        await browser.close()
