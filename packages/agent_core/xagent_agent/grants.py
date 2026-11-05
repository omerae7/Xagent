"""Finite scoped permissions: opt-in only, checked again immediately before effects.

Payment, login, arbitrary code, unknown/destructive web actions and security changes
remain per-operation decisions. A grant never overrides a current policy denial,
mission restriction, employee boundary, cancellation, or owner takeover.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from xagent_core.audit import audit
from xagent_core.db import utcnow
from xagent_core.models import Approval, Mission, PermissionGrant, PermissionUse, Run
from xagent_core.security import _derive


def policy_revision(rule) -> str:  # noqa: ANN001
    value = {k: getattr(rule, k, None) for k in ("mode", "conditions", "rule_text", "updated_at")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def scope_for(tool: str, category: str, args: dict, effect: dict | None) -> dict | None:
    """No payload/credentials stored. Exact selector + observed DOM target are bound.

    Native Telegram grants bind a recipient; browser grants bind one observed
    element and its exact URL/form/effect metadata. Never treat all typing as send.
    """
    if tool == "telegram_send" and category == "external_message" and args.get("chat") and not args.get("file"):
        key = {"tool": tool, "category": category, "chat": str(args["chat"]).lstrip("@").lower()}
        public = {"recipient": key["chat"], "action": "send_message"}
    elif tool == "browser_type" and category in {"desktop_control", "external_message"} and effect:
        target = effect.get("target") or {}
        if target.get("sensitive") or category == "desktop_control" and args.get("submit"):
            return None
        url = str(effect.get("url") or "")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        key = {"tool": tool, "category": category, "effect": effect,
               "target": {k: args.get(k) for k in ("ref", "selector", "label", "submit")}}
        public = {"domain": parsed.hostname, "action": "submit_message" if args.get("submit") else "edit_draft",
                  "element": target.get("label") or target.get("tag") or "editable"}
    else:
        return None
    digest = hmac.new(_derive("xagent-scoped-grant-v1"),
                      json.dumps(key, sort_keys=True, default=str).encode(), hashlib.sha256).hexdigest()
    return {"hash": digest, "target": public}


async def mission_key(session, run: Run) -> str:  # noqa: ANN001
    mid = (run.state or {}).get("mission_id")
    if mid:
        return str(mid)
    root = run
    for _ in range(8):
        if not root.parent_run_id:
            return str(root.id)
        root = await session.get(Run, root.parent_run_id)
        if root is None:
            break
    return str(run.id)  # broken lineage never broadens a grant


async def create(session, approval: Approval, actor: str, kind: str, *, days: int, max_uses: int):  # noqa: ANN001
    from . import harness
    run = await session.get(Run, approval.run_id)
    meta = (approval.args or {}).get("__grant")
    if kind not in {"mission", "standing"} or not meta or not run or (run.state or {}).get("requested_by") != actor:
        raise ValueError("این عملیات فقط با اجازه یک‌باره قابل اجراست")
    rules = await harness.load_rules(session, fresh=True)
    revision = policy_revision(rules.get(approval.category))
    if meta.get("policy_revision") != revision:
        raise ValueError("سیاست پس از درخواست تغییر کرده است؛ اجازه تازه لازم است")
    grant = PermissionGrant(owner=actor, employee_id=run.employee_id, source_approval_id=approval.id, kind=kind,
                            mission_key=await mission_key(session, run) if kind == "mission" else None,
                            tool_name=approval.tool_name, category=approval.category, scope_hash=meta["hash"],
                            scope=meta["target"], policy_revision=revision,
                            expires_at=utcnow() + timedelta(days=days), max_uses=max_uses, uses=0)
    session.add(grant)
    await session.flush()
    await audit(session, actor, "permission.created", str(grant.id), {"scope": grant.scope, "kind": kind})
    return grant


async def find(session, run: Run, tool: str, category: str, scope: dict | None, rule,  # noqa: ANN001
               cid: str, *, consume: bool = False):
    owner = (run.state or {}).get("requested_by")
    if not scope or not owner or not (run.state or {}).get("trusted"):
        return None
    stmt = select(PermissionGrant).where(
        PermissionGrant.owner == owner, PermissionGrant.employee_id == run.employee_id,
        PermissionGrant.tool_name == tool, PermissionGrant.category == category,
        PermissionGrant.scope_hash == scope["hash"], PermissionGrant.revoked_at.is_(None),
        PermissionGrant.expires_at > utcnow(), PermissionGrant.policy_revision == policy_revision(rule),
    ).order_by(PermissionGrant.created_at)
    if consume:
        stmt = stmt.with_for_update()
    rows = (await session.execute(stmt.execution_options(populate_existing=True))).scalars().all()
    key = await mission_key(session, run)
    for grant in rows:
        if grant.kind == "mission" and grant.mission_key != key:
            continue
        if grant.kind == "mission":
            mid = (run.state or {}).get("mission_id")
            if mid:
                mission = await session.get(Mission, uuid.UUID(str(mid)), populate_existing=True)
                if not mission or mission.status != "active" or mission.expires_at and mission.expires_at <= utcnow():
                    continue
            else:
                status = run.status if key == str(run.id) else (await session.execute(
                    select(Run.status).where(Run.id == uuid.UUID(key)))).scalar_one_or_none()
                if status not in {"queued", "running", "waiting_approval", "paused"}:
                    continue
        used = (await session.execute(select(PermissionUse).where(
            PermissionUse.grant_id == grant.id, PermissionUse.run_id == run.id,
            PermissionUse.tool_call_id == cid))).scalar_one_or_none()
        if grant.uses >= grant.max_uses and used is None:
            continue
        if consume and used is None:
            grant.uses += 1
            session.add(PermissionUse(grant_id=grant.id, run_id=run.id, tool_call_id=cid))
            await session.flush()
        return grant
    return None
