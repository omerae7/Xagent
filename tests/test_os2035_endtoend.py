"""Real panel -> API -> Run/checkpoint -> approval -> agent tools -> actual DOM.

Only model output is scripted. No production accounts, external messages or model bills.
Worker delivery is explicitly driven by AgentRunner, not a 24/7 Worker soak test.
"""

# ruff: noqa: F811 (imported pytest fixtures)
import shutil
import uuid

import pytest
import web_harness
from autonomy_helpers import body_text, called, tool_names
from browser_env import CHROME
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from test_followup_browser_continuity import comp, env  # noqa: F401
from test_forensic_s6_outage_ui import build_app
from test_master_worker import live_api

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not CHROME or not web_harness.available(), reason="browser/web dependencies unavailable"
    ),
]


async def test_real_panel_approval_resumes_exact_form_and_reports_verified_result(
    app_client, configured_model, mock_llm, comp, env
):
    from playwright.async_api import async_playwright

    from xagent_agent.pipeline import AgentRunner
    from xagent_api.main import app

    marker = "PANEL-E2E-" + uuid.uuid4().hex[:6]

    def handler(body):
        if marker not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if "browser_type" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        done = [n for n, _ in called(body)]
        if "browser_open" not in done:
            return ("browser_open", {"url": env.web + "/editor"})
        if "browser_type" not in done:
            return (
                "browser_type",
                {
                    "selector": "#draft",
                    "value": "synthetic UI approval",
                    "submit": True,
                    "expect_text": "Saved locally 1",
                },
            )
        return "فرم محلی ارسال شد و رسید تازهٔ Saved locally 1 مشاهده شد."

    folder = build_app(web_harness.WEB)

    class SPA(StaticFiles):
        async def get_response(self, path, scope):
            try:
                return await super().get_response(path, scope)
            except HTTPException as e:
                if e.status_code != 404:
                    raise
                return FileResponse(folder + "/index.html")

    app.mount("/", SPA(directory=folder, html=True), name="synthetic-ui")
    route = app.routes[-1]
    mock_llm.handlers.insert(0, handler)
    try:
        async with live_api() as base, async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            await page.goto(base + "/chat")
            await page.locator('textarea, input[type=password]').first.wait_for()
            if await page.locator("input[type=password]").count():
                # Use the real form and owner authentication endpoint.
                await page.locator('input[autocomplete=username]').fill("admin")
                await page.locator("input[type=password]").fill("Test-Password-123")
                await page.get_by_role("button", name="ورود", exact=False).click()
            await page.locator("textarea").first.wait_for()
            await page.locator("textarea").first.fill("با مرورگر فرم را پر کن و ارسال کن " + marker)
            async with page.expect_response(
                lambda r: "/api/chat/send" in r.url and r.request.method == "POST"
            ) as response:
                await page.locator("textarea").first.press("Enter")
            rid = uuid.UUID((await (await response.value).json())["run"]["id"])
            runner = AgentRunner("real-panel")
            await runner.execute(rid)
            approve = page.get_by_role("button", name="اجازه می‌دهم", exact=True)
            await approve.wait_for(timeout=15000)
            target = comp._sessions[str(rid)]
            assert await target.evaluate("window.sends || 0") == 0
            async with page.expect_response(
                lambda r: "/api/approvals/" in r.url and r.request.method == "POST"
            ) as response:
                await approve.click()
            assert (await response.value).status == 200
            await runner.execute(rid)
            await page.get_by_text("فرم محلی ارسال شد و رسید تازهٔ", exact=False).wait_for(timeout=15000)
            assert not target.is_closed()
            assert await target.input_value("#draft") == "synthetic UI approval"
            assert await target.evaluate("window.sends") == 1
            await page.get_by_role("button", name="مراحل اجرا", exact=False).click()
            await page.get_by_text("تکمیل با شواهد تأیید شد", exact=True).wait_for()
            await browser.close()
    finally:
        app.routes.remove(route)
        mock_llm.handlers.remove(handler)
        shutil.rmtree(folder)
