"""مأموریت جامع — مرحله ۸: G4 (بازگشایی Run با وضعیت و شواهد پایدار) و پنهان‌بودن اثرانگشت تأیید در API."""

from __future__ import annotations

import uuid

from xagent_core.db import session_scope
from xagent_core.models import Approval, Run


async def test_G4_reopened_run_shows_durable_status_and_evidence(app_client, configured_model, runner, mock_llm):
    r = await app_client.post("/api/chat/send", json={"text": "یک فایل یادداشت بساز (G4-OUTCOME) فایل"})
    rid = uuid.UUID(r.json()["run"]["id"])
    await runner.execute(rid)
    first = (await app_client.get(f"/api/runs/{rid}")).json()
    again = (await app_client.get(f"/api/runs/{rid}")).json()
    assert first["run"]["status"] == again["run"]["status"]
    out = again["outcome"]
    assert set(out) >= {"validation", "evidence", "delivery", "settled"}
    if again["run"]["status"] == "succeeded":
        assert out["validation"]["passed"] is True and out["evidence"], "اجرای موفق بدون شواهد نمایش داده شد"
    assert "state" not in again["run"], "State خام اجرا از API نشت کرد"


async def test_approval_binding_is_not_exposed(app_client):
    async with session_scope() as s:
        run = Run(trigger="chat", input="x", status="waiting_approval", state={})
        s.add(run)
        await s.flush()
        s.add(Approval(run_id=run.id, tool_call_id="c1", category="external_message", tool_name="telegram_send",
                       args={"chat": "ali", "__binding": {"sig": "secret-fingerprint"}}))
        rid = run.id
    detail = (await app_client.get(f"/api/runs/{rid}")).text
    listing = (await app_client.get("/api/approvals")).text
    assert "secret-fingerprint" not in detail and "secret-fingerprint" not in listing
