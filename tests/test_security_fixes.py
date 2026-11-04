"""آزمون‌های بازتولید و اصلاح ایرادات امنیتی ۱ تا ۵ و ۸ (پیش از Merge).

هر آزمون ابتدا روی کد قبلی اجرا شد و شکست خورد (بازتولید)، سپس اصلاح انجام شد.
۱) Viewer و کارمند دسترسی مدیریتی ندارند  ۲) SSRF: Redirect، DNS rebinding، مقصد نهایی
۳) Rollback مجوز را دوباره بررسی می‌کند    ۴) سقف کارمند مقید به سقف مالک
۵) کنترل هزینه مدل بدون قیمت و قفل قیمت     ۸) ایزوله‌بودن واقعی حافظه خصوصی کارمند
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from sqlalchemy import select

from xagent_core.db import session_scope
from xagent_core.models import AIModel, Approval, Employee, Run, SettingKV, UsageEvent, User
from xagent_core.security import hash_password


# ───────────── کمک‌ها ─────────────
async def _viewer_client():
    from xagent_api.main import app

    async with session_scope() as s:
        if (await s.execute(select(User).where(User.username == "viewer1"))).scalar_one_or_none() is None:
            s.add(User(username="viewer1", password_hash=hash_password("Viewer-Pass-123"), role="viewer"))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-XAgent": "1"})
    r = await c.post("/api/auth/login", json={"username": "viewer1", "password": "Viewer-Pass-123"})
    assert r.status_code == 200, r.text
    c.cookies.set("xa_token", r.json()["token"])
    return c


def _ctx(session, *, trusted=True, principal="owner", employee=None, scope=None, run_id=None):
    from xagent_tools.base import ToolContext

    async def _pub(*_a, **_k):
        return None

    extra = {"principal": principal, "approved": set(), "employee_name": employee}
    if scope:
        extra["memory_scopes"] = [scope]
        extra["memory_write_scope"] = scope
    return ToolContext(session=session, run_id=run_id, conversation_id=None, timezone="Asia/Tehran",
                       trusted_owner=trusted, sandbox=None, publish=_pub, extra=extra)


# ───────────── ۱) احراز هویت مالک ─────────────
async def test_viewer_cannot_use_management_endpoints(app_client, configured_model):
    viewer = await _viewer_client()
    try:
        # مشاهده مجاز است
        assert (await viewer.get("/api/dashboard")).status_code == 200
        # ساخت اجرای ایجنت (دسترسی به ابزارهای مدیریتی) ممنوع
        r = await viewer.post("/api/chat/send", json={"text": "تنظیمات را عوض کن"})
        assert r.status_code == 403, r.text
        # ساخت کارت اجازه واقعی توسط مالک و تلاش Viewer برای تأیید
        data = (await app_client.post("/api/chat/send", json={"text": "سلام"})).json()
        async with session_scope() as s:
            ap = Approval(run_id=uuid.UUID(data["run"]["id"]), tool_call_id="c1", category="security_limits",
                          tool_name="settings_update", args={}, reason="آزمون")
            s.add(ap)
            await s.flush()
            aid = str(ap.id)
        r = await viewer.post(f"/api/approvals/{aid}", json={"decision": "approve"})
        assert r.status_code == 403, r.text
        async with session_scope() as s:
            assert (await s.get(Approval, uuid.UUID(aid))).status == "pending"
        # افزایش بودجه اجرا، نوشتن در حافظه و ساخت تسک
        assert (await viewer.post(f"/api/runs/{data['run']['id']}/resume", json={"extra_budget_usd": 5})).status_code == 403
        assert (await viewer.post("/api/memory/facts", json={"section": "owner", "key": "x", "content": "y"})).status_code == 403
        assert (await viewer.post("/api/tasks", json={"title": "x"})).status_code == 403
        assert (await viewer.post("/api/voice/tts", json={"text": "سلام"})).status_code == 403
    finally:
        await viewer.aclose()


async def test_employee_principal_cannot_call_admin_tools(app_client, configured_model):
    from xagent_agent.control import ops_tools

    async with session_scope() as s:
        ctx = _ctx(s, trusted=True, principal="employee", employee="researcher")
        with pytest.raises(PermissionError):
            await ops_tools.settings_update(ctx, {"section": "general", "values": {"agent_name": "نفوذ"}, "reason": "x"})
        untrusted = _ctx(s, trusted=False, principal="external")
        with pytest.raises(PermissionError):
            await ops_tools.employee_upsert(untrusted, {"name": "evil", "tool_groups": ["admin"]})


async def test_run_without_principal_is_not_trusted():
    from xagent_agent.pipeline import run_is_owner

    assert run_is_owner({}) is False  # پیش‌فرض امن: بدون هویت = غیرقابل‌اعتماد
    assert run_is_owner({"trusted": True, "principal": "employee"}) is False
    assert run_is_owner({"trusted": True, "principal": "owner"}) is True


# ───────────── ۲) SSRF ─────────────
class _PublicIP(ipaddress.IPv4Address):
    """127.0.0.2 در آزمون نقش یک IP عمومی را بازی می‌کند."""

    is_private = is_loopback = is_link_local = is_reserved = is_multicast = is_unspecified = False
    is_global = True


@pytest.fixture
def fake_public_ip(monkeypatch):
    # بدون پروکسی محیطی (در CI و سرور واقعی پروکسی وجود ندارد)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    real = ipaddress.ip_address
    monkeypatch.setattr(ipaddress, "ip_address", lambda a: _PublicIP(str(a)) if str(a) == "127.0.0.2" else real(a))


def _serve(host: str, handler) -> tuple[HTTPServer, int]:
    srv = HTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class _Secret(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"INTERNAL-SECRET-PAYLOAD")

    def log_message(self, *a):
        pass


async def test_ssrf_redirect_to_internal_is_blocked(fake_public_ip, monkeypatch):
    from xagent_tools.builtin import web

    secret, sport = _serve("127.0.0.1", _Secret)

    class _Redirect(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sport}/admin")
            self.end_headers()

        def log_message(self, *a):
            pass

    red, rport = _serve("127.0.0.2", _Redirect)
    real_gai = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, *a, **k: real_gai("127.0.0.2" if h in ("redirect.test", b"redirect.test") else h, *a, **k))
    try:
        async with session_scope() as s:
            try:
                res = await web.web_fetch(_ctx(s), {"url": f"http://redirect.test:{rport}/start"})
                out = res.output
            except (PermissionError, ValueError) as e:
                out = str(e)
        assert "INTERNAL-SECRET-PAYLOAD" not in out
    finally:
        red.shutdown()
        secret.shutdown()


async def test_ssrf_dns_rebinding_connects_only_to_vetted_ip(fake_public_ip, monkeypatch):
    from xagent_tools.builtin import web

    secret, sport = _serve("127.0.0.1", _Secret)
    calls = {"n": 0}
    real_gai = socket.getaddrinfo

    def rebinding(host, *a, **k):
        if host in ("rebind.test", b"rebind.test"):
            calls["n"] += 1
            return real_gai("127.0.0.2" if calls["n"] == 1 else "127.0.0.1", *a, **k)
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    try:
        async with session_scope() as s:
            try:
                res = await web.web_fetch(_ctx(s), {"url": f"http://rebind.test:{sport}/"})
                out = res.output
            except Exception as e:  # noqa: BLE001
                out = str(e)
        assert "INTERNAL-SECRET-PAYLOAD" not in out
        assert calls["n"] == 1, "نام دامنه فقط یک بار و پیش از اتصال حل شود"
    finally:
        secret.shutdown()


async def test_ssrf_blocks_mapped_and_numeric_internal_addresses():
    from xagent_tools.net import UnsafeDestination, validate_ip

    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1", "fd00::1"):
        with pytest.raises(UnsafeDestination):
            validate_ip(ip)


# ───────────── ۳) Rollback و مجوز ─────────────
async def test_manager_cannot_rollback_owner_change_or_loosen_limits(app_client, configured_model):
    from xagent_agent.control import changes

    sl = (await app_client.get("/api/settings")).json()["settings"]["stoploss"]
    sl["daily_cost_usd"] = 2.0
    assert (await app_client.put("/api/settings/stoploss", json=sl)).status_code == 200  # تغییر مالک
    async with session_scope() as s:
        from xagent_core.models import ConfigChange

        owner_ch = (await s.execute(select(ConfigChange).where(ConfigChange.target_id == "stoploss", ConfigChange.actor == "owner")
                                    .order_by(ConfigChange.created_at.desc()))).scalars().first()
        with pytest.raises(changes.ChangeError) as ei:
            await changes.rollback(s, owner_ch.id, actor="manager", reason="آزمون")
        assert ei.value.decision in ("approval", "forbidden")
    async with session_scope() as s:
        assert (await s.get(SettingKV, "stoploss")).value["daily_cost_usd"] == 2.0
        # مدیر سقف را سخت‌تر می‌کند (مجاز) و سپس با Rollback شل می‌کند (باید نیازمند اجازه باشد)
        ch = await changes.change_setting(s, "stoploss", {"daily_cost_usd": 1.0}, actor="manager", reason="صرفه‌جویی")
        with pytest.raises(changes.ChangeError) as ei:
            await changes.rollback(s, ch.id, actor="manager", reason="برگرداندن")
        assert ei.value.decision == "approval"
    async with session_scope() as s:
        assert (await s.get(SettingKV, "stoploss")).value["daily_cost_usd"] == 1.0
        res = await changes.rollback(s, ch.id, actor="owner", reason="مالک")
        assert res.reverted
    sl["daily_cost_usd"] = 5.0
    assert (await app_client.put("/api/settings/stoploss", json=sl)).status_code == 200


# ───────────── ۴) سقف کارمند ─────────────
async def test_employee_limits_are_bounded_by_owner_caps(app_client, configured_model):
    from xagent_agent.control import changes

    async with session_scope() as s:
        with pytest.raises(changes.ChangeError) as ei:
            await changes.upsert_employee(s, {"name": "spender", "max_cost_usd": 15, "max_steps": 90}, actor="manager",
                                          reason="آزمون")
        assert ei.value.decision in ("forbidden", "approval")
    async with session_scope() as s:
        assert (await s.execute(select(Employee).where(Employee.name == "spender"))).scalar_one_or_none() is None


async def test_delegated_budget_never_exceeds_owner_cap_or_parent_budget(app_client, configured_model, runner, mock_llm):
    from xagent_agent.employees import employee_budget
    from xagent_core.settings_store import StopLossSettings

    sl = StopLossSettings()
    emp = Employee(name="x", max_steps=100, max_cost_usd=Decimal("20"))
    cost, steps = employee_budget(emp, sl, parent_remaining=Decimal("0.05"))
    assert cost <= 0.05 and cost <= sl.employee_max_cost_usd
    assert steps <= sl.employee_max_steps
    cost, _ = employee_budget(emp, sl, parent_remaining=None)
    assert cost == sl.employee_max_cost_usd


# ───────────── ۵) مدل بدون قیمت ─────────────
async def test_unpriced_model_is_costed_conservatively(app_client, configured_model, runner, mock_llm):
    pid = (await app_client.get("/api/settings/ai/providers")).json()["providers"][0]["id"]
    m = (await app_client.post("/api/settings/ai/models", json={"provider_id": pid, "model_name": "mock-unpriced",
                                                                "capabilities": {"tools": True}})).json()
    ai = (await app_client.get("/api/settings")).json()["settings"]["ai"]
    saved = {k: list(v) for k, v in ai["smart"].items() if isinstance(v, list)}
    ai["smart"]["manager_model_ids"] = [m["id"]]
    ai["smart"]["worker_model_ids"] = [m["id"]]
    ai["smart"]["manager_enabled"] = ai["smart"]["worker_enabled"] = [True]
    assert (await app_client.put("/api/settings/ai", json=ai)).status_code == 200
    try:
        data = (await app_client.post("/api/chat/send", json={"text": "سلام با مدل بدون قیمت"})).json()
        await runner.execute(uuid.UUID(data["run"]["id"]))
        async with session_scope() as s:
            evs = (await s.execute(select(UsageEvent).where(UsageEvent.model_id == uuid.UUID(m["id"])))).scalars().all()
            assert evs, "مصرف باید ثبت شود"
            assert all(Decimal(e.cost_usd) > 0 for e in evs), "مدل بدون قیمت نباید رایگان حساب شود"
            assert all(e.cost_estimated for e in evs)
    finally:
        ai["smart"].update(saved)
        await app_client.put("/api/settings/ai", json=ai)


async def test_manager_cannot_lower_model_price(app_client, configured_model):
    from xagent_agent.control import changes

    async with session_scope() as s:
        with pytest.raises(changes.ChangeError) as ei:
            await changes.change_model(s, configured_model, {"price_in": 0, "price_out": 0, "pricing_source": "manual"},
                                       actor="manager", reason="ارزان‌تر")
        assert ei.value.decision == "forbidden"
    async with session_scope() as s:
        m = await s.get(AIModel, uuid.UUID(configured_model))
        assert Decimal(m.price_in) == Decimal("1.0")


# ───────────── ۸) حافظه خصوصی کارمند ─────────────
async def test_private_employee_memory_is_isolated(app_client, configured_model):
    from xagent_tools.builtin import core

    private_scope = f"employee:{uuid.uuid4()}"
    async with session_scope() as s:
        await core.memory_remember(_ctx(s, principal="employee", employee="secretive", scope=private_scope),
                                   {"section": "owner", "key": "private-note", "content": "رمز گاوصندوق دفتر ۷۷۴۲ است"})
    async with session_scope() as s:
        manager = await core.memory_search(_ctx(s), {"query": "رمز گاوصندوق دفتر"})
        assert "۷۷۴۲" not in manager.output
        other = await core.memory_search(_ctx(s, principal="employee", employee="other", scope=f"employee:{uuid.uuid4()}"),
                                         {"query": "رمز گاوصندوق دفتر"})
        assert "۷۷۴۲" not in other.output
        own = await core.memory_search(_ctx(s, principal="employee", employee="secretive", scope=private_scope),
                                       {"query": "رمز گاوصندوق دفتر"})
        assert "۷۷۴۲" in own.output


async def test_private_employee_learning_is_scoped(app_client, configured_model):
    from xagent_agent.learning import memory_scope_for
    from xagent_core.models import Run as RunModel

    emp = Employee(id=uuid.uuid4(), name="p", memory_scope="private")
    shared = Employee(id=uuid.uuid4(), name="s", memory_scope="shared")
    assert memory_scope_for(RunModel(employee_id=emp.id), emp) == f"employee:{emp.id}"
    assert memory_scope_for(RunModel(employee_id=shared.id), shared) == "global"
    assert memory_scope_for(RunModel(), None) == "global"


_ = Run  # جلوگیری از هشدار import بلااستفاده در برخی نسخه‌ها
