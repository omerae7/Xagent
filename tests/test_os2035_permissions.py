"""Scoped authorization, durable PostgreSQL claims, real local browser effects."""

# ruff: noqa: F811 (imported pytest fixtures)
import uuid
from datetime import timedelta

import pytest
from autonomy_helpers import body_text, called, tool_names
from sqlalchemy import select
from test_followup_browser_continuity import comp, env  # noqa: F401

from xagent_agent import grants, harness
from xagent_core.db import session_scope, utcnow
from xagent_core.models import PermissionGrant, PermissionUse, Run

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "scenario", ["valid", "revoked", "expired", "employee", "owner", "mission", "policy", "scope", "limit"]
)
async def test_grant_scope_and_use_time_conditions(app_client, scenario):
    args = {"chat": "@synthetic", "text": "safe payload"}
    scope = grants.scope_for("telegram_send", "external_message", args, None)
    async with session_scope() as s:
        rule = (await harness.load_rules(s))["external_message"]
        run = Run(trigger="chat", input="synthetic", state={"trusted": True, "requested_by": "admin"})
        s.add(run)
        await s.flush()
        grant = PermissionGrant(
            owner="admin",
            employee_id=None,
            source_approval_id=uuid.uuid4(),
            kind="standing",
            tool_name="telegram_send",
            category="external_message",
            scope_hash=scope["hash"],
            scope=scope["target"],
            policy_revision=grants.policy_revision(rule),
            expires_at=utcnow() + timedelta(days=1),
            max_uses=1,
            uses=0,
        )
        if scenario == "revoked":
            grant.revoked_at = utcnow()
        if scenario == "expired":
            grant.expires_at = utcnow() - timedelta(seconds=1)
        if scenario == "employee":
            grant.employee_id = uuid.uuid4()
        if scenario == "owner":
            grant.owner = "another-owner"
        if scenario == "mission":
            grant.kind, grant.mission_key = "mission", str(uuid.uuid4())
        if scenario == "policy":
            grant.policy_revision = "outdated"
        if scenario == "scope":
            grant.scope_hash = "another-target"
        if scenario == "limit":
            grant.uses = 1
        s.add(grant)
        await s.flush()
        found = await grants.find(
            s, run, "telegram_send", "external_message", scope, rule, "one", consume=True
        )
        assert (found is not None) == (scenario == "valid")
        if scenario == "valid":
            assert grant.uses == 1
            assert await grants.find(
                s, run, "telegram_send", "external_message", scope, rule, "one", consume=True
            )
            assert grant.uses == 1  # repeated use-time check is not another authorized effect
            assert not await grants.find(
                s, run, "telegram_send", "external_message", scope, rule, "two", consume=True
            )
            assert (
                len(
                    (await s.execute(select(PermissionUse).where(PermissionUse.grant_id == grant.id)))
                    .scalars()
                    .all()
                )
                == 1
            )
        await s.rollback()  # isolate all synthetic permissions


async def test_dangerous_categories_and_file_sends_have_no_reusable_grant():
    for category in ("payment", "login", "web_irreversible", "code_exec", "security_limits"):
        assert (
            grants.scope_for("browser_type", category, {"submit": True}, {"url": "https://example.com"})
            is None
        )
    assert (
        grants.scope_for("telegram_send", "external_message", {"chat": "@a", "file": "/private"}, None)
        is None
    )


@pytest.mark.parametrize("kind", ["standing", "mission"])
async def test_approved_browser_action_reused_in_scope_then_revoked(
    app_client, configured_model, mock_llm, comp, env, kind
):
    from xagent_agent.pipeline import AgentRunner
    from xagent_core.models import Approval

    mark = "SCOPED-BROWSER-" + kind

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
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
                    "value": "synthetic",
                    "submit": True,
                    "expect_text": "Saved locally 1",
                },
            )
        return "Synthetic form effect observed."

    mock_llm.handlers.insert(0, handler)
    ids = []
    parent = None
    if kind == "mission":
        async with session_scope() as s:
            root = Run(
                trigger="chat",
                input="Synthetic active mission",
                status="running",
                state={"trusted": True, "requested_by": "admin"},
            )
            s.add(root)
            await s.flush()
            parent = root.id

    async def new_run(parent=None):
        async with session_scope() as s:
            r = Run(
                trigger="chat",
                input="با مرورگر فرم را ارسال کن " + mark,
                parent_run_id=parent,
                state={"trusted": True, "requested_by": "admin"},
                status="queued",
            )
            s.add(r)
            await s.flush()
            ids.append(r.id)
            return r.id

    try:
        runner = AgentRunner("scoped")
        first = await new_run(parent)
        await runner.execute(first)
        async with session_scope() as s:
            ap = (
                await s.execute(
                    select(Approval).where(Approval.run_id == first, Approval.status == "pending")
                )
            ).scalar_one()
            aid = ap.id
        response = await app_client.post(f"/api/approvals/{aid}", json={"decision": "approve", "scope": kind})
        assert response.status_code == 200, response.text
        assert (
            await app_client.post(f"/api/approvals/{aid}", json={"decision": "approve", "scope": kind})
        ).status_code == 409
        await runner.execute(first)
        async with session_scope() as s:
            r = await s.get(Run, first)
            assert r.status == "succeeded", r.result
            grant = (
                await s.execute(select(PermissionGrant).where(PermissionGrant.source_approval_id == aid))
            ).scalar_one()
            gid = grant.id
        second = await new_run(parent)
        # Reset only the synthetic local form's receipt, preserving its target identity.
        page = next(p for p in (await comp._context()).pages if p.url == env.web + "/editor")
        await page.reload()
        await runner.execute(second)
        async with session_scope() as s:
            r = await s.get(Run, second)
            assert r.status == "succeeded", r.result
            assert (await s.get(PermissionGrant, gid)).uses == 1
        assert await page.evaluate("window.sends") == 1
        listing = await app_client.get("/api/settings/permissions")
        assert listing.status_code == 200
        assert "scope_hash" not in listing.text and "policy_revision" not in listing.text
        assert (await app_client.post(f"/api/settings/permissions/{gid}/revoke")).status_code == 200
        await page.reload()
        third = await new_run(parent)
        await runner.execute(third)
        async with session_scope() as s:
            assert (await s.get(Run, third)).status == "waiting_approval"
        assert await page.evaluate("window.sends || 0") == 0
    finally:
        mock_llm.handlers.remove(handler)
        if parent:
            async with session_scope() as s:
                (await s.get(Run, parent)).status = "succeeded"


async def test_two_runs_cannot_exceed_shared_permission_limit(app_client):
    import asyncio

    scope = grants.scope_for("telegram_send", "external_message", {"chat": "concurrent-synthetic"}, None)
    async with session_scope() as s:
        rule = (await harness.load_rules(s))["external_message"]
        run_ids = []
        for _ in range(2):
            r = Run(trigger="chat", input="synthetic", state={"trusted": True, "requested_by": "admin"})
            s.add(r)
            await s.flush()
            run_ids.append(r.id)
        g = PermissionGrant(
            owner="admin",
            employee_id=None,
            source_approval_id=uuid.uuid4(),
            kind="standing",
            tool_name="telegram_send",
            category="external_message",
            scope_hash=scope["hash"],
            scope=scope["target"],
            policy_revision=grants.policy_revision(rule),
            expires_at=utcnow() + timedelta(days=1),
            max_uses=1,
            uses=0,
        )
        s.add(g)
        await s.flush()
        gid = g.id

    async def consume(rid):
        async with session_scope() as s:
            r = await s.get(Run, rid)
            rule = (await harness.load_rules(s))["external_message"]
            found = await grants.find(
                s, r, "telegram_send", "external_message", scope, rule, "call", consume=True
            )
            return found is not None

    assert sorted(await asyncio.gather(*(consume(rid) for rid in run_ids))) == [False, True]
    async with session_scope() as s:
        assert (await s.get(PermissionGrant, gid)).uses == 1


async def test_unsupported_reusable_permission_rolls_back_the_decision(app_client):
    from xagent_core.models import Approval

    async with session_scope() as s:
        run = Run(
            trigger="chat",
            input="synthetic",
            status="waiting_approval",
            state={"trusted": True, "requested_by": "admin"},
        )
        s.add(run)
        await s.flush()
        ap = Approval(
            run_id=run.id,
            tool_call_id="sensitive",
            category="login",
            tool_name="browser_type",
            args={},
            status="pending",
        )
        s.add(ap)
        await s.flush()
        aid, rid = ap.id, run.id
    r = await app_client.post(f"/api/approvals/{aid}", json={"decision": "approve", "scope": "standing"})
    assert r.status_code == 422
    async with session_scope() as s:
        assert (await s.get(Approval, aid)).status == "pending"
        assert (await s.get(Run, rid)).status == "waiting_approval"


async def test_viewer_cannot_inspect_or_revoke_owner_grants(app_client):
    import httpx

    from xagent_api.main import app
    from xagent_core.models import User
    from xagent_core.security import create_access_token, hash_password

    async with session_scope() as s:
        viewer = User(
            username="permission-viewer-" + uuid.uuid4().hex[:6],
            role="viewer",
            password_hash=hash_password("SyntheticOnly123!"),
        )
        s.add(viewer)
        await s.flush()
        token = create_access_token(str(viewer.id), viewer.token_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token, "X-XAgent": "1"},
    ) as client:
        assert (await client.get("/api/settings/permissions")).status_code == 403
        assert (await client.post(f"/api/settings/permissions/{uuid.uuid4()}/revoke")).status_code == 403


async def test_completed_mission_cannot_authorize_later_child_work(app_client):
    scope = grants.scope_for("telegram_send", "external_message", {"chat": "completed-synthetic"}, None)
    async with session_scope() as s:
        rules = await harness.load_rules(s)
        parent = Run(
            trigger="chat",
            input="finished",
            status="succeeded",
            state={"trusted": True, "requested_by": "admin"},
        )
        s.add(parent)
        await s.flush()
        child = Run(
            trigger="delegate",
            input="late work",
            parent_run_id=parent.id,
            state={"trusted": True, "requested_by": "admin"},
        )
        s.add(child)
        await s.flush()
        grant = PermissionGrant(
            owner="admin",
            employee_id=None,
            source_approval_id=uuid.uuid4(),
            kind="mission",
            mission_key=str(parent.id),
            tool_name="telegram_send",
            category="external_message",
            scope_hash=scope["hash"],
            scope=scope["target"],
            policy_revision=grants.policy_revision(rules["external_message"]),
            expires_at=utcnow() + timedelta(days=1),
            max_uses=100,
            uses=0,
        )
        s.add(grant)
        await s.flush()
        assert not await grants.find(
            s,
            child,
            "telegram_send",
            "external_message",
            scope,
            rules["external_message"],
            "new",
            consume=True,
        )
        await s.rollback()
