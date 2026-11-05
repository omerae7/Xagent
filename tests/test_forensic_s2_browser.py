"""ممیزی جرم‌شناختی — مرحله ۲، F04: طبقه‌بندی اثر کلیک/ارسال بر اساس عنصر واقعی DOM (ref/selector/text یکسان).

Chromium واقعی و صفحه محلی؛ هیچ ارسال/خرید واقعی انجام نمی‌شود (کلیک‌ها فقط روی صفحه آزمون محلی‌اند).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from browser_env import CHROME, reset_computer, start

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست")]

PAGE = """<!doctype html><html><head><title>Controls</title></head><body><h1>Shop admin</h1>
<p>Manage items, orders and posts on this page.</p>
<button id="c1">Send</button>
<button id="c2" aria-label="Delete item"><svg width="16" height="16"><rect width="16" height="16"/></svg></button>
<button id="c3" class="btn">Publish</button>
<form action="/checkout" onsubmit="return false"><button type="submit" id="c4">Continue</button></form>
<form action="/orders" onsubmit="return false"><input type="submit" id="c5" value="Submit order"></form>
<button id="c6" data-action="remove-account">Proceed</button>
<button id="c7" onclick="document.title='viewed'">View details</button>
<form action="/search" onsubmit="return false"><input id="q" name="q" placeholder="Search"></form>
</body></html>"""

# (شناسه، متن برای هدف‌گیری متنی، دسته مورد انتظار؛ None یعنی عادی)
CASES = [
    ("c1", "Send", "external_message"),
    ("c2", "Delete item", "web_irreversible"),
    ("c3", "Publish", "external_message"),
    ("c4", "Continue", "payment"),
    ("c5", "Submit order", "web_irreversible"),
    ("c6", "Proceed", "web_irreversible"),
    ("c7", "View details", None),
]


@pytest.fixture(scope="module")
def env():
    e, stop = start({"/admin/": (200, {}, PAGE)})
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


def ctx():
    return SimpleNamespace(run_id=uuid.uuid4(), extra={"employee_id": None, "approved": set()}, vision=None,
                           sandbox=None, trusted_owner=True)


async def _category(comp, c, target: dict) -> str | None:
    from xagent_tools.errors import EscalateCategory

    try:
        await comp.browser_click(c, target)
    except EscalateCategory as e:
        return e.category
    return None


async def _refs(comp, c) -> dict[str, int]:
    page = comp._sessions[str(c.run_id)]
    await comp._elements(page)
    return dict(await page.evaluate(
        "() => [...document.querySelectorAll('[data-xa-id]')].filter(e => e.id).map(e => [e.id, +e.dataset.xaId])"))


@pytest.mark.parametrize("method", ["ref", "selector", "text"])
async def test_F04_same_control_same_category_for_every_addressing_method(comp, env, method):
    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/admin/", "reuse": False})
    refs = await _refs(comp, c)
    wrong = []
    for el_id, text, expected in CASES:
        target = {"ref": refs[el_id]} if method == "ref" else (
            {"selector": f"#{el_id}"} if method == "selector" else {"text": text})
        got = await _category(comp, c, target)
        if got != expected:
            wrong.append((el_id, method, expected, got))
    assert not wrong, f"طبقه‌بندی بر اساس رشته هدف، نه عنصر واقعی: {wrong}"


async def test_F04_ambiguous_selector_with_sensitive_match_requires_approval(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/admin/", "reuse": False})
    assert await _category(comp, c, {"selector": "button"}) is not None, "انتخابگر مبهم شامل «Send» بدون تأیید کلیک شد"


async def test_F04_ordinary_control_still_clicks_and_approved_effect_proceeds(comp, env):
    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/admin/", "reuse": False})
    res = await comp.browser_click(c, {"selector": "#c7"})
    assert res.ok and await comp._sessions[str(c.run_id)].title() == "viewed"  # مثبت: کلیک عادی بدون تأیید
    c.extra["approved"] = {"external_message"}
    assert (await comp.browser_click(c, {"selector": "#c1"})).ok  # با تأیید همان دسته


async def test_F04_type_submit_classifies_by_real_form(comp, env):
    from xagent_tools.errors import EscalateCategory

    c = ctx()
    await comp.browser_open(c, {"url": f"{env.web}/admin/", "reuse": False})
    res = await comp.browser_type(c, {"selector": "#q", "value": "chair", "submit": True})
    assert res.ok  # فرم جست‌وجو: عادی
    with pytest.raises(EscalateCategory):
        await comp.browser_type(c, {"selector": "#c5", "value": "x", "submit": True})
