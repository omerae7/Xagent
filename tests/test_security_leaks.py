"""آزمون الزامات امنیتی: عدم افشای اسرار در پاسخ API، دیتابیس، رخدادها، لاگ و STT؛ ماندگاری پس از راه‌اندازی مجدد؛
جست‌وجوی وب بدون دسکتاپ از مسیر کامل اجرای ایجنت."""

from __future__ import annotations

import io
import json
import logging
import uuid

from sqlalchemy import select

from xagent_core.db import dispose_engine, session_scope
from xagent_core.models import AIModel, Employee, Incident, Message, Run, RunStep, SettingKV, UsageEvent
from xagent_core.redact import MASK, SecretScrubFilter, register_secret, scrub

# کلید با قالب غیراستاندارد: فقط لایه «مقدار دقیق ثبت‌شده» می‌تواند آن را بگیرد
ODD_KEY = "zq-custom-5ecret-9f8e7d6c5b4a3f2e"


def test_scrub_known_formats_and_registered_values():
    samples = {
        "OpenAI sk-proj-AbCdEf0123456789xyz rejected": "sk-proj-AbCdEf0123456789xyz",
        "anthropic sk-ant-api03-ZZZZZZZZZZZZZZZZ": "sk-ant-api03-ZZZZZZZZZZZZZZZZ",
        "google https://x.googleapis.com/v1?key=AIzaSyA1234567890abcdefghijkl&alt=json": "AIzaSyA1234567890abcdefghijkl",
        "Authorization: Bearer abcdef1234567890token": "abcdef1234567890token",
        "bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0 failed": "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0",
        "postgresql://xagent:SuperSecretPw@postgres:5432/xagent": "SuperSecretPw",
        '{"api_key": "plainvalue12345"}': "plainvalue12345",
        "groq gsk_abcdefghijklmnop1234 invalid": "gsk_abcdefghijklmnop1234",
    }
    for text, secret in samples.items():
        out = scrub(text)
        assert secret not in out, out
        assert MASK in out
    register_secret("unusual-format-value-77")
    assert "unusual-format-value-77" not in scrub("provider said: unusual-format-value-77 is wrong")
    # متن عادی فارسی و شناسه مدل دست‌نخورده می‌ماند
    assert scrub("مدل gpt-4o-mini پاسخ نداد") == "مدل gpt-4o-mini پاسخ نداد"


def test_log_filter_scrubs_message_args_and_tracebacks():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(SecretScrubFilter())
    log = logging.getLogger("xagent.test.leak")
    log.addHandler(h)
    log.propagate = False
    try:
        register_secret(ODD_KEY)
        log.error("provider failed with %s", ODD_KEY)
        try:
            raise RuntimeError(f"boom sk-live-0123456789abcdefXYZ {ODD_KEY}")
        except RuntimeError:
            log.exception("unexpected")
    finally:
        log.removeHandler(h)
    out = buf.getvalue()
    assert ODD_KEY not in out and "sk-live-0123456789abcdefXYZ" not in out
    assert "provider failed with" in out and "RuntimeError" in out


async def _echo_provider(app_client, mock_llm, caps: dict) -> tuple[str, str]:
    r = await app_client.post("/api/settings/ai/providers", json={
        "kind": "custom", "name": "Echo", "base_url": mock_llm.base_url.replace("/v1", "/echo/v1"), "api_key": ODD_KEY})
    assert r.status_code == 200, r.text
    assert ODD_KEY not in r.text
    pid = r.json()["id"]
    m = await app_client.post("/api/settings/ai/models", json={"provider_id": pid, "model_name": "echo-model", "capabilities": caps})
    assert m.status_code == 200, m.text
    return pid, m.json()["id"]


async def test_provider_error_echoing_key_never_leaks(app_client, configured_model, runner, mock_llm):
    pid, mid = await _echo_provider(app_client, mock_llm, {"tools": True})
    # ۱) آزمون اتصال از پنل
    r = await app_client.post(f"/api/settings/ai/models/{mid}/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert ODD_KEY not in r.text, r.text
    # ۲) فهرست ارائه‌دهندگان فقط کلید ماسک‌شده دارد
    r = await app_client.get("/api/settings/ai/providers")
    assert ODD_KEY not in r.text
    # ۳) اجرای واقعی با مدل خراب به‌عنوان مدیر و کارمند (بدون Fallback)
    ai = (await app_client.get("/api/settings")).json()["settings"]["ai"]
    saved = json.loads(json.dumps(ai))
    ai["smart"]["manager_model_ids"] = [mid]
    ai["smart"]["manager_enabled"] = [True]
    ai["smart"]["worker_model_ids"] = [mid]
    ai["smart"]["worker_enabled"] = [True]
    assert (await app_client.put("/api/settings/ai", json=ai)).status_code == 200
    try:
        data = (await app_client.post("/api/chat/send", json={"text": "سلام با کلید خراب"})).json()
        await runner.execute(uuid.UUID(data["run"]["id"]))
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(data["run"]["id"]))
            m = await s.get(AIModel, uuid.UUID(mid))
            stored = [run.error or "", run.result or "", m.last_error or ""]
            stored += [i.message for i in (await s.execute(select(Incident))).scalars()]
            stored += [u.error or "" for u in (await s.execute(select(UsageEvent).where(UsageEvent.model_id == uuid.UUID(mid)))).scalars()]
            stored += [x.content or "" for x in (await s.execute(select(Message).where(Message.conversation_id == uuid.UUID(data["conversation"]["id"])))).scalars()]
            stored += [st.title + json.dumps(st.detail or {}, ensure_ascii=False) for st in
                       (await s.execute(select(RunStep).where(RunStep.run_id == run.id))).scalars()]
        assert m.health == "failing" and m.last_error, "خطا باید ثبت شود"
        for txt in stored:
            assert ODD_KEY not in txt, txt[:300]
        # ۴) پاسخ‌های API که خطاها را نمایش می‌دهند
        for url in ("/api/control/incidents", "/api/settings/ai/providers", f"/api/chat/conversations/{data['conversation']['id']}/messages",
                    f"/api/chat/runs/{data['run']['id']}", "/api/dashboard", "/api/control/changes"):
            r = await app_client.get(url)
            assert ODD_KEY not in r.text, url
        r = await app_client.post("/api/control/diagnose")
        assert ODD_KEY not in r.text
    finally:
        assert (await app_client.put("/api/settings/ai", json=saved)).status_code == 200
        await app_client.delete(f"/api/settings/ai/providers/{pid}")


async def test_stt_failure_does_not_leak_key(app_client, configured_model, mock_llm):
    pid, mid = await _echo_provider(app_client, mock_llm, {"stt": True, "tools": False})
    try:
        r = await app_client.post("/api/voice/stt", files={"file": ("a.webm", b"\x1a\x45\xdf\xa3fake-audio", "audio/webm")})
        assert r.status_code == 502, r.text
        assert ODD_KEY not in r.text and "تبدیل گفتار ناموفق" in r.json()["detail"]
    finally:
        await app_client.delete(f"/api/settings/ai/providers/{pid}")


async def test_manager_changes_survive_restart(app_client, configured_model, runner, mock_llm):
    """تغییر ثبت‌شده باید پس از بسته شدن همه اتصال‌ها (معادل راه‌اندازی مجدد فرایند) از دیتابیس خوانده شود."""
    r = await app_client.post("/api/control/employees", json={"name": "persist-check", "title": "آزمون ماندگاری",
                                                             "tool_groups": ["web"], "max_steps": 7})
    assert r.status_code == 200, r.text
    g = (await app_client.get("/api/settings")).json()["settings"]["general"]
    g["agent_name"] = "ایجنت ماندگار"
    assert (await app_client.put("/api/settings/general", json=g)).status_code == 200
    await dispose_engine()  # همه اتصال‌ها و Session ها بسته می‌شوند؛ اتصال بعدی از نو ساخته می‌شود
    async with session_scope() as s:
        e = (await s.execute(select(Employee).where(Employee.name == "persist-check"))).scalar_one()
        assert e.max_steps == 7 and e.tool_groups == ["web"]
        assert (await s.get(SettingKV, "general")).value["agent_name"] == "ایجنت ماندگار"
    assert (await app_client.get("/api/settings")).json()["settings"]["general"]["agent_name"] == "ایجنت ماندگار"


async def test_web_search_runs_without_desktop(app_client, configured_model, runner, mock_llm, monkeypatch):
    """جست‌وجوی وب فقط با HTTP انجام می‌شود؛ دسکتاپ در دسترس نیست و اجرا باید موفق باشد."""
    from xagent_core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "desktop_url", "http://127.0.0.1:9")
    seen: list[str] = []
    from xagent_tools import net

    async def fake_safe_request(method, url, **kw):
        import httpx

        seen.append(url)
        html = ('<div class="result"><a class="result__a" href="https://example.org/coin">قیمت سکه امروز</a>'
                '<a class="result__snippet">سکه امامی امروز ثبت شد</a></div>')
        return net.SafeResponse(200, url, httpx.Headers({"content-type": "text/html"}), html.encode())

    monkeypatch.setattr(net, "safe_request", fake_safe_request)
    mock_llm.scenarios["قیمت سکه را از وب"] = [("web_search", {"query": "قیمت سکه امروز"})]
    try:
        data = (await app_client.post("/api/chat/send", json={"text": "قیمت سکه را از وب پیدا کن"})).json()
        await runner.execute(uuid.UUID(data["run"]["id"]))
        async with session_scope() as ss:
            run = await ss.get(Run, uuid.UUID(data["run"]["id"]))
            steps = (await ss.execute(select(RunStep).where(RunStep.run_id == run.id, RunStep.tool_name == "web_search"))).scalars().all()
        assert run.status == "succeeded", run.error
        assert steps and steps[0].status == "ok", [(x.status, x.detail) for x in steps]
        assert seen and "duckduckgo" in seen[0]
        assert "قیمت سکه" in (run.result or "")
    finally:
        mock_llm.scenarios.pop("قیمت سکه را از وب", None)
