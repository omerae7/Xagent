"""ممیزی جرم‌شناختی — مرحله ۲، F05: جداسازی هویت مرورگر کارمندان (Chromium واقعی، صفحه محلی).

کوکی، LocalStorage و SessionStorage هر کارمند جداست؛ کارفرما همان Context پایدار خود را دارد؛
پس از «ری‌استارت» (قطع و اتصال دوباره CDP و پاک شدن حافظه فرایند) هویت هر کارمند از فایل رمزشده بازمی‌گردد.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from browser_env import CHROME, reset_computer, start

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chromium در دسترس نیست")]

ID_PAGE = """<!doctype html><html><head><title>Identity</title></head><body><h1>Account state</h1>
<p id="out">loading</p><script>
const p = new URLSearchParams(location.search), v = p.get('set');
if (v) { document.cookie = 'who=' + v + '; path=/; max-age=86400'; localStorage.setItem('who', v);
         sessionStorage.setItem('who', v); }
const c = (document.cookie.match(/who=([^;]+)/) || [])[1] || 'none';
document.getElementById('out').textContent = 'cookie=' + c + ' local=' + (localStorage.getItem('who') || 'none')
  + ' session=' + (sessionStorage.getItem('who') || 'none');
</script></body></html>"""


@pytest.fixture(scope="module")
def env():
    e, stop = start({"/id/": (200, {}, ID_PAGE)})
    yield e
    stop()


@pytest.fixture
async def comp(env, monkeypatch, tmp_path):
    from xagent_tools.builtin import computer

    monkeypatch.setattr(computer, "_cdp_url", lambda: env.cdp)

    async def no_guard():
        return None
    monkeypatch.setattr(computer, "_guard", no_guard)
    monkeypatch.setattr(computer, "READY_TIMEOUT_MS", 3000)
    monkeypatch.setattr(computer, "_identity_path", lambda emp: tmp_path / f"{emp}.state")
    computer._browser = computer._pw = None
    computer._identities.clear()
    yield computer
    await reset_computer(computer)
    computer._identities.clear()


def ctx(employee=None, session=None):
    return SimpleNamespace(run_id=uuid.uuid4(), extra={"employee_id": employee, "approved": set()}, vision=None,
                           sandbox=None, trusted_owner=employee is None, session=session)


async def visit(comp, c, env, set_to: str | None = None) -> str:
    q = f"?set={set_to}" if set_to else ""
    res = await comp.browser_open(c, {"url": f"{env.web}/id/{q}", "reuse": False})
    await comp.release_session(c.run_id)
    return res.output


async def test_F05_employees_and_owner_do_not_share_cookies_or_storage(comp, env):
    a, b, owner = "emp-A", "emp-B", None
    assert "cookie=A-val" in await visit(comp, ctx(a), env, "A-val")
    out_b = await visit(comp, ctx(b), env)
    assert "A-val" not in out_b, f"کارمند B کوکی/حافظه کارمند A را دید: {out_b}"
    assert "B-val" in await visit(comp, ctx(b), env, "B-val")
    out_owner = await visit(comp, ctx(owner), env)
    assert "A-val" not in out_owner and "B-val" not in out_owner, "هویت کارمند به Context کارفرما نشت کرد"
    await visit(comp, ctx(owner), env, "OWNER-val")
    out_a = await visit(comp, ctx(a), env)
    assert "cookie=A-val" in out_a and "local=A-val" in out_a and "OWNER-val" not in out_a and "B-val" not in out_a


async def test_F05_identity_survives_restart_encrypted_and_stays_isolated(comp, env, tmp_path):
    await visit(comp, ctx("emp-A"), env, "A-keep")
    await visit(comp, ctx("emp-B"), env, "B-keep")
    files = list(tmp_path.glob("*.state"))
    assert files and all(b"A-keep" not in f.read_bytes() for f in files), "وضعیت هویت به‌صورت متن ساده ذخیره شد"
    await reset_computer(comp)  # ری‌استارت: اتصال CDP و Contextها از بین می‌روند
    comp._identities.clear()
    out_a = await visit(comp, ctx("emp-A"), env)
    assert "cookie=A-keep" in out_a and "local=A-keep" in out_a and "B-keep" not in out_a, out_a
    assert "session=none" in out_a  # SessionStorage طبق تعریف به تب تازه منتقل نمی‌شود
    out_b = await visit(comp, ctx("emp-B"), env)
    assert "cookie=B-keep" in out_b and "A-keep" not in out_b


async def test_F05_owner_session_shared_only_by_explicit_scoped_policy(comp, env):
    from xagent_core.db import session_scope
    from xagent_core.settings_store import BrowserSharingSettings, set_setting

    await visit(comp, ctx(None), env, "OWNER-login")
    async with session_scope() as s:
        await set_setting(s, "browser_sharing", BrowserSharingSettings(shares={"emp-A": ["127.0.0.1"]}))
    try:
        async with session_scope() as s:
            assert "cookie=OWNER-login" in await visit(comp, ctx("emp-A", s), env), "اشتراک صریح میزبان اعمال نشد"
            out_b = await visit(comp, ctx("emp-B", s), env)
            assert "OWNER-login" not in out_b, "کارمند بدون سیاست اشتراک نشست کارفرما را دید"
    finally:
        async with session_scope() as s:
            await set_setting(s, "browser_sharing", BrowserSharingSettings())


async def test_F05_sharing_policy_is_owner_only():
    from xagent_agent.control import policy

    assert policy.check_setting("browser_sharing", {}, {"shares": {"e": ["x"]}}, "manager").decision == "forbidden"
    assert policy.check_setting("browser_sharing", {}, {"shares": {"e": ["x"]}}, "owner").decision == "allow"


async def test_F05_corrupt_identity_requires_reauthentication_not_foreign_state(comp, env, tmp_path):
    await visit(comp, ctx("emp-A"), env, "A-old")
    await visit(comp, ctx("emp-B"), env, "B-old")
    (tmp_path / "emp-A.state").write_bytes(b"not-a-valid-token")
    await reset_computer(comp)
    comp._identities.clear()
    out = await visit(comp, ctx("emp-A"), env)
    assert "cookie=none" in out and "B-old" not in out, "هویت خراب به هویت دیگری یا Context کارفرما افتاد"
