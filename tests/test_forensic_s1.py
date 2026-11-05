"""ممیزی جرم‌شناختی — مرحله ۱: F01 (افشای اسرار از API نسخه‌ها) و F21 (نشت رمز از مشاهدات مرورگر).

همه اسرار ساختگی‌اند (`dummy-…`)؛ هیچ اعتبارنامه واقعی استفاده نمی‌شود.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from test_security_fixes import _viewer_client

from xagent_core.db import session_scope
from xagent_core.models import ConfigChange, InstalledTool, MemoryRevision, ToolVersion

ENV_SECRET = "dummy-ENV-tok-9f3a1c7e5b2d"
NEW_SECRET = "dummy-NEW-tok-4c8e1a7f3b9d"
HDR_SECRET = "dummy-HDR-7c1e2a9d6b3f"
URL_SECRET = "dummy-URL-pw-2b7d9e1c"
SECRETS = (ENV_SECRET, NEW_SECRET, HDR_SECRET, URL_SECRET)


async def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    from xagent_memory.revisions import record_change, snapshot

    async with session_scope() as s:
        t = InstalledTool(name=f"f01-{uuid.uuid4().hex[:8]}", kind="mcp_http", status="ready", enabled=True,
                          source_url=f"https://bot:{URL_SECRET}@git.example/tool.git",
                          config={"transport": "http", "url": "https://mcp.example/", "headers": {"Authorization":
                                  f"Bearer {HDR_SECRET}"}, "env": {"GITHUB_TOKEN": ENV_SECRET, "LOG_LEVEL": "info"}})
        s.add(t)
        await s.flush()
        await record_change(s, "tool", t, "create", None, "نصب", actor="owner", origin="panel")
        before = snapshot("tool", t)
        t.config = {**t.config, "env": {"GITHUB_TOKEN": NEW_SECRET, "LOG_LEVEL": "debug"}}
        rev = await record_change(s, "tool", t, "update", before, "چرخش کلید", actor="owner", origin="panel")
        s.add(ToolVersion(tool_id=t.id, version="v1", path="/tmp/f01", manifest={"env": {"API_KEY": ENV_SECRET}},
                          test_status="passed", test_output=f"connected with token={ENV_SECRET}"))
        s.add(ConfigChange(actor="owner", target_type="tool", target_id=str(t.id), action="update",
                           summary=f"headers Authorization: Bearer {HDR_SECRET}", reason="x",
                           before={"config": {"env": {"GITHUB_TOKEN": ENV_SECRET}}},
                           after={"api_key": NEW_SECRET, "password": URL_SECRET}))
        return t.id, rev.id


def _leaks(text: str) -> list[str]:
    return [x for x in SECRETS if x in text]


async def test_F01_viewer_cannot_read_tool_secrets_from_any_api(app_client):
    tid, _ = await _seed()
    viewer = await _viewer_client()
    try:
        for url in ("/api/memory/revisions", "/api/memory/revisions?entity_type=tool", "/api/control/changes",
                    "/api/tools", f"/api/tools/{tid}/versions"):
            r = await viewer.get(url)
            assert r.status_code == 200, (url, r.status_code)
            assert not _leaks(r.text), f"Viewer اسرار را از {url} خواند: {_leaks(r.text)}"
    finally:
        await viewer.aclose()


async def test_F01_owner_api_also_masks_raw_secrets_but_keeps_structure(app_client):
    tid, _ = await _seed()
    for url in ("/api/memory/revisions?entity_type=tool", "/api/control/changes", f"/api/tools/{tid}/versions"):
        r = await app_client.get(url)
        assert r.status_code == 200 and not _leaks(r.text), (url, _leaks(r.text))
    items = (await app_client.get("/api/memory/revisions?entity_type=tool")).json()["items"]
    mine = next(i for i in items if i["entity_id"] == str(tid) and i["action"] == "update")
    env = mine["after"]["config"]["env"]
    assert set(env) == {"GITHUB_TOKEN", "LOG_LEVEL"} and env["GITHUB_TOKEN"] != NEW_SECRET  # کلیدها می‌مانند، مقدار نه


async def test_F01_snapshots_at_rest_are_sealed_and_undo_still_restores(app_client):
    tid, rid = await _seed()
    async with session_scope() as s:
        revs = (await s.execute(select(MemoryRevision).where(MemoryRevision.entity_id == tid))).scalars().all()
        raw = " ".join(str(r.before) + str(r.after) for r in revs)
    assert not _leaks(raw), "Snapshot تازه ابزار اسرار را به‌صورت متن ساده در جدول نسخه‌ها نگه داشت"
    r = await app_client.post(f"/api/memory/revisions/{rid}/undo", json={"reason": "آزمون"})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        tool = await s.get(InstalledTool, tid)
        assert tool.config["env"]["GITHUB_TOKEN"] == ENV_SECRET, "Undo پیکربندی واقعی ابزار را بازنگرداند"
        assert tool.config["headers"]["Authorization"] == f"Bearer {HDR_SECRET}"


async def test_F01_sse_event_payload_is_redacted():
    from xagent_api.routers.live import sse_frame

    frame = sse_frame({"type": "memory.revision", "data": {"reason": f"token={ENV_SECRET}",
                                                          "config": {"env": {"X": NEW_SECRET}}}})
    assert not _leaks(frame), frame


async def test_F01_secret_inventory_is_read_only_and_prints_no_values(app_client, capsys):
    from xagent_api.cli import secrets_inventory

    await _seed()
    async with session_scope() as s:
        before = (await s.execute(select(MemoryRevision))).scalars().all()
        n_before = len(before)
    report = await secrets_inventory(dispose=False)
    out = capsys.readouterr().out
    assert not _leaks(out) and not _leaks(str(report)), "فهرست اسرار مقدار سری را چاپ کرد"
    assert report["findings"] and all({"table", "id", "path", "kind"} <= set(f) for f in report["findings"])
    async with session_scope() as s:
        assert len((await s.execute(select(MemoryRevision))).scalars().all()) == n_before, "فهرست‌برداری داده را تغییر داد"


async def test_F01_unauthenticated_and_employee_tool_paths_do_not_leak(app_client):
    from types import SimpleNamespace

    import httpx

    from xagent_agent.control import ops_tools

    tid, _ = await _seed()
    anon = httpx.AsyncClient(transport=app_client._transport, base_url=str(app_client.base_url))
    try:
        for url in ("/api/memory/revisions", "/api/control/changes", f"/api/tools/{tid}/versions"):
            r = await anon.get(url)
            assert r.status_code == 401 and not _leaks(r.text), url
    finally:
        await anon.aclose()
    async with session_scope() as s:
        res = await ops_tools.change_history(SimpleNamespace(session=s, extra={}), {"limit": 50})
    assert res.ok and not _leaks(res.output), "ابزار کارمند (تاریخچه تغییرات) اسرار را به مدل نشان داد"
