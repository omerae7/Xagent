"""Actual DOM editable targeting; synthetic local form, no user accounts."""

# ruff: noqa: F811 (imported pytest fixtures)
import pytest
from test_followup_browser_continuity import comp, ctx, env  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_auto_selects_unique_editable_and_verifies_value(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": env.web + "/editor"})
    r = await comp.browser_type(c, {"value": "متن بدون ارسال"})
    assert r.ok and r.verified is True
    page = comp._sessions[str(c.run_id)]
    assert await page.input_value("#draft") == "متن بدون ارسال"
    assert await page.evaluate("window.sends || 0") == 0


async def test_contenteditable_selected_by_accessible_label_not_message_text(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": env.web + "/editor"})
    page = comp._sessions[str(c.run_id)]
    await page.set_content(
        '<h1>Synthetic messenger</h1><div contenteditable="true" role="textbox" aria-label="Message composer"></div>'
    )
    r = await comp.browser_type(c, {"label": "Message composer", "value": "پیام ساختگی"})
    assert r.ok and r.verified is True
    assert await page.get_by_role("textbox").inner_text() == "پیام ساختگی"


async def test_ambiguous_and_stale_targets_fail_before_any_fill(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": env.web + "/editor"})
    page = comp._sessions[str(c.run_id)]
    await page.set_content('<h1>Two identities</h1><input id="a"><input id="b">')
    for args in ({"value": "no"}, {"selector": "#missing", "value": "no"}):
        r = await comp.browser_type(c, args)
        assert not r.ok and not r.data.get("outcome_uncertain")
    assert await page.input_value("#a") == await page.input_value("#b") == ""


async def test_login_submission_cannot_reuse_a_message_permission(comp, env):
    from xagent_tools.errors import EscalateCategory

    c = ctx()
    c.extra["approved"] = {"external_message", "desktop_control"}
    await comp.browser_open(c, {"url": env.web + "/editor"})
    page = comp._sessions[str(c.run_id)]
    await page.set_content(
        '<h1>Account</h1><form action="/login"><input id="username"><button>Sign in</button></form>'
    )
    with pytest.raises(EscalateCategory) as esc:
        await comp.browser_type(c, {"selector": "#username", "value": "synthetic", "submit": True})
    assert esc.value.category == "login"
    assert await page.input_value("#username") == ""


async def test_missing_click_does_not_start_an_effect(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": env.web + "/editor"})
    result = await comp.browser_click(c, {"ref": 99999})
    assert not result.ok and result.outcome.value == "NOT_STARTED"
    assert await comp._sessions[str(c.run_id)].evaluate("window.sends || 0") == 0
