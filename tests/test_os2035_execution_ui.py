"""Actual React execution timeline and permission controls in local Chromium."""

import shutil

import pytest
import web_harness
from browser_env import CHROME, start

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not CHROME or not web_harness.available(), reason="browser/web dependencies unavailable"
    ),
]
ENTRY = """
import {createRoot} from 'react-dom/client';
import {StepList,ApprovalCard} from '@web/components/chat';
import {stepState} from '@web/components/ui/ExecutionStatus';
(window as any).__STATES=[stepState({kind:'tool',status:'error',detail:{outcome:'NOT_STARTED',verified:false}}),stepState({kind:'tool',status:'ok',detail:{outcome:'OBSERVED'}})];
const steps=Array.from({length:1000},(_,i)=>({id:String(i),kind:'tool',status:'ok',title:'Operation '+i,detail:{verified: i===999 ? true:undefined}}));
steps[998].detail={outcome:'UNKNOWN_EFFECT'} as any;
createRoot(document.getElementById('root')!).render(<><StepList steps={steps}/><ApprovalCard a={{id:'synthetic',tool_name:'telegram_send',category:'external_message',grant_options:['once','mission','standing'],grant_scope:{recipient:'synthetic'}}} /></>);
"""


@pytest.fixture(scope="module")
def execution_ui():
    folder, _ = web_harness.build(ENTRY)
    env, stop = start(web_harness.routes_for(folder))
    yield env.web
    stop()
    shutil.rmtree(folder)


async def test_large_timeline_is_bounded_and_effect_states_are_distinct(execution_ui):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(reduced_motion="reduce")
        await page.goto(execution_ui)
        await page.locator('[data-state=VERIFIED]').wait_for()
        assert await page.locator("ol > li").count() == 6
        assert await page.evaluate("window.__STATES") == ["NOT_STARTED", "OBSERVED"]
        assert await page.locator("[data-state=EXECUTED]").count() == 4
        assert await page.locator("[data-state=VERIFIED]").count() == 1
        assert await page.locator("[data-state=UNKNOWN_EFFECT]").count() == 1
        await page.get_by_role("button", name="نمایش همهٔ 1000 مرحله و شواهد").click()
        assert await page.locator("ol > li").count() == 1000
        await page.get_by_role("button", name="نمایش خلاصهٔ اجرا").click()
        assert await page.locator("ol > li").count() == 6
        await browser.close()


async def test_approval_scope_is_explicit_and_sent_to_api(execution_ui):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page()
        posted = []

        async def approval(route):
            posted.append(route.request.post_data_json)
            await route.fulfill(json={"status": "approved"})

        await page.route("**/api/approvals/synthetic", approval)
        await page.goto(execution_ui)
        select = page.get_by_label("دامنه اجازه", exact=True)
        await select.wait_for()
        assert await select.input_value() == "once"
        await select.select_option("standing")
        await page.get_by_text("متن‌های جدید در همین اقدام", exact=False).wait_for()
        await page.get_by_role("button", name="اجازه می‌دهم", exact=True).click()
        await page.wait_for_timeout(100)
        assert posted == [{"decision": "approve", "scope": "standing"}]
        await browser.close()
