"""ممیزی جرم‌شناختی — مرحله ۱، F21: نشت رمز/مقدار حساس از مشاهدات مرورگر (Chromium واقعی محلی).

صفحه‌ها محلی و کنترل‌شده‌اند؛ همه مقادیر ساختگی‌اند. الزام تأیید «ورود» (login) تضعیف نمی‌شود.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from autonomy_helpers import body_text, called, tool_names
from browser_env import CHROME, reset_computer, start
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست")]

PREFILL_PW = "dummy-PW-prefill-7731"
PREFILL_OTP = "dummy-OTP-998877"
PREFILL_USER = "dummy-user-alice"
TYPED_PW = "dummy-PW-typed-2468"
TYPED_OTP = "dummy-OTP-typed-5511"
ALL = (PREFILL_PW, PREFILL_OTP, PREFILL_USER, TYPED_PW, TYPED_OTP)
LOGIN = f"""<!doctype html><html><head><title>Sign in</title></head><body>
<h1>Account sign in</h1><p>Please sign in to continue to your dashboard.</p>
<form><input name="username" value="{PREFILL_USER}" placeholder="Email">
<input type="password" name="pw" value="{PREFILL_PW}" placeholder="Password">
<input name="otp_code" value="{PREFILL_OTP}" placeholder="One-time code">
<input type="number" name="otp_digits" placeholder="کد یکبارمصرف">
<input type="submit" value="Sign in"><button type="button" id="help">Help</button></form></body></html>"""


@pytest.fixture(scope="module")
def env():
    e, stop = start({"/login/": (200, {}, LOGIN)})
    yield e
    stop()


@pytest.fixture
async def comp(env, monkeypatch):
    from xagent_tools.builtin import computer

    monkeypatch.setattr(computer, "_cdp_url", lambda: env.cdp)

    async def no_guard():
        return None
    monkeypatch.setattr(computer, "_guard", no_guard)
    monkeypatch.setattr(computer, "READY_TIMEOUT_MS", 3000)
    computer._browser = computer._pw = None
    yield computer
    await reset_computer(computer)


def ctx(approved=()):
    return SimpleNamespace(run_id=uuid.uuid4(), extra={"employee_id": None, "approved": set(approved)}, vision=None,
                           sandbox=None, trusted_owner=True)


def leaks(text: str) -> list[str]:
    return [v for v in ALL if v in text]


async def test_F21_observations_never_contain_prefilled_sensitive_values(comp, env):
    c = ctx()
    opened = await comp.browser_open(c, {"url": f"{env.web}/login/", "reuse": False})
    assert opened.data["page_state"] == "auth" and not leaks(opened.output), leaks(opened.output)
    els = await comp._elements(comp._sessions[str(c.run_id)])
    assert not leaks(els), leaks(els)
    assert "حساس" in els and "Sign in" in els  # دکمه برچسب دارد؛ فیلد رمز/OTP پرچم حساس
    read = await comp.browser_read(c, {})
    assert not leaks(read.output) and not leaks(json.dumps(read.data, ensure_ascii=False))
    click = await comp.browser_click(c, {"selector": "#help"})
    assert click.ok and not leaks(click.output)
    await comp._elements(comp._sessions[str(c.run_id)])  # ارجاع‌ها: ۱ نام کاربری، ۲ رمز
    info = await comp._describe(comp._sessions[str(c.run_id)], {"ref": 2})
    assert info and info["sensitive"] is True and PREFILL_PW not in json.dumps(info)


async def test_F21_password_typing_still_requires_login_approval(comp, env):
    from xagent_tools.errors import EscalateCategory

    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/login/", "reuse": False})
    with pytest.raises(EscalateCategory) as ex:
        await comp.browser_type(c, {"selector": "input[name=pw]", "value": TYPED_PW})
    assert ex.value.category == "login" and TYPED_PW not in str(ex.value.reason) + str(ex.value.extra_args)


async def test_F21_approved_password_typing_works_without_echo(comp, env):
    c = ctx(approved=("login",))
    await comp.browser_open(c, {"url": f"{env.web}/login/", "reuse": False})
    res = await comp.browser_type(c, {"selector": "input[name=pw]", "value": TYPED_PW})
    assert res.ok and res.data["sensitive_input"] is True and not leaks(res.output)
    page = comp._sessions[str(c.run_id)]
    assert await page.input_value("input[name=pw]") == TYPED_PW  # مثبت: ورود واقعاً انجام شد
    from xagent_core.redact import scrub_known

    assert TYPED_PW not in scrub_known(f"echo {TYPED_PW} back"), "مقدار حساس تایپ‌شده برای پاک‌سازی ثبت نشد"


async def test_F21_error_path_does_not_carry_sensitive_value(comp, env):
    c = ctx(approved=("login",))
    await comp.browser_open(c, {"url": f"{env.web}/login/", "reuse": False})
    res = await comp.browser_type(c, {"selector": "input[name=otp_digits]", "value": TYPED_OTP})
    assert not res.ok and res.data["sensitive_input"] is True and not leaks(res.output), res.output


# ── Pipeline: رونوشت، Checkpoint، گام‌ها و کارت تأیید بدون مقدار حساس؛ پس از تأیید، ورود انجام می‌شود ──
async def test_F21_pipeline_transcript_checkpoint_steps_and_approval_are_clean(app_client, configured_model, mock_llm,
                                                                               comp, env):
    from xagent_agent.pipeline import AgentRunner
    from xagent_core.db import session_scope
    from xagent_core.models import Approval, Run, RunStep

    mark = "F21-PIPE"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        names, done = tool_names(body), [n for n, _ in called(body)]
        if "browser_type" not in names:
            return ("tools_load", {"groups": ["browser"]})
        if "browser_open" not in done:
            return ("browser_open", {"url": f"{env.web}/login/", "reuse": False})
        if "browser_type" not in done:
            return ("browser_type", {"ref": 2, "value": TYPED_PW})  # ارجاع عددی: حساسیت فقط از DOM معلوم است
        return "وارد فیلد رمز شدم."
    async with session_scope() as s:
        run = Run(trigger="chat", input=f"با مرورگر وارد حساب شو ({mark})", status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    runner = AgentRunner("f21")
    try:
        await runner.execute(rid)

        async def dump() -> str:
            async with session_scope() as s:
                r = await s.get(Run, rid)
                steps = (await s.execute(select(RunStep).where(RunStep.run_id == rid))).scalars().all()
                aps = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalars().all()
                return json.dumps({"state": r.state, "result": r.result, "error": r.error,
                                   "steps": [st.detail for st in steps], "approvals": [a.args for a in aps]},
                                  ensure_ascii=False, default=str), r.status, aps
        text, status, aps = await dump()
        assert status == "waiting_approval", status
        assert not leaks(text), f"مقدار حساس در Checkpoint/گام/تأیید ذخیره شد: {leaks(text)}"
        r = await app_client.post(f"/api/approvals/{aps[0].id}", json={"decision": "approve"})
        assert r.status_code == 200, r.text
        assert TYPED_PW not in r.text
        await runner.execute(rid)
        text, status, _ = await dump()
        assert not leaks(text), f"پس از اجرا مقدار حساس ذخیره شد: {leaks(text)}"
        page = comp._sessions.get(str(rid))
        assert page is None or page.is_closed() or await page.input_value("input[name=pw]") == TYPED_PW
        assert status in ("succeeded", "failed"), status
    finally:
        mock_llm.handlers.remove(handler)
