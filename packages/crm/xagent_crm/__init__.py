"""مدیریت ارتباط با همکاران، مخاطبان و پروژه‌ها (CRM)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import Contact, CRMInteraction, Project, Task
from xagent_core.text import normalize_fa

CONTACT_FIELDS = ("kind", "name", "role_title", "company", "phone", "email", "telegram_username", "telegram_id", "notes", "tags")


async def find_contacts(session: AsyncSession, query: str, limit: int = 10) -> list[Contact]:
    q = normalize_fa(query).lstrip("@")
    if not q:
        return []
    pat = f"%{q}%"
    stmt = (
        select(Contact)
        .where(
            or_(
                func.xa_normalize(Contact.name).like(pat),
                func.lower(Contact.telegram_username) == q,
                Contact.phone.like(pat),
                func.lower(Contact.email).like(pat),
                func.xa_normalize(Contact.company).like(pat),
                func.similarity(func.xa_normalize(Contact.name), q) > 0.3,
            )
        )
        .order_by(func.similarity(func.xa_normalize(Contact.name), q).desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def contact_by_telegram(session: AsyncSession, telegram_id: int | None, username: str | None) -> Contact | None:
    conds = []
    if telegram_id:
        conds.append(Contact.telegram_id == telegram_id)
    if username:
        conds.append(func.lower(Contact.telegram_username) == username.lower().lstrip("@"))
    if not conds:
        return None
    return (await session.execute(select(Contact).where(or_(*conds)).limit(1))).scalar_one_or_none()


async def upsert_contact(session: AsyncSession, data: dict[str, Any], contact_id: uuid.UUID | None = None) -> Contact:
    c: Contact | None = None
    if contact_id:
        c = await session.get(Contact, contact_id)
    if c is None and (data.get("telegram_username") or data.get("telegram_id")):
        c = await contact_by_telegram(session, data.get("telegram_id"), data.get("telegram_username"))
    if c is None and data.get("name"):
        found = (
            await session.execute(
                select(Contact).where(func.xa_normalize(Contact.name) == normalize_fa(data["name"])).limit(1)
            )
        ).scalar_one_or_none()
        c = found
    if c is None:
        c = Contact(name=data.get("name") or "بی‌نام")
        session.add(c)
    for f in CONTACT_FIELDS:
        if f in data and data[f] is not None:
            v = data[f]
            if f == "telegram_username" and isinstance(v, str):
                v = v.lstrip("@")
            setattr(c, f, v)
    await session.flush()
    return c


async def log_interaction(
    session: AsyncSession,
    contact_id: uuid.UUID | None,
    summary: str,
    channel: str = "note",
    direction: str = "in",
    content: str = "",
    project_id: uuid.UUID | None = None,
    follow_up_at: datetime | None = None,
) -> CRMInteraction:
    it = CRMInteraction(
        contact_id=contact_id,
        project_id=project_id,
        channel=channel,
        direction=direction,
        summary=summary,
        content=content,
        follow_up_at=follow_up_at,
    )
    session.add(it)
    if contact_id:
        c = await session.get(Contact, contact_id)
        if c:
            c.last_contact_at = utcnow()
    await session.flush()
    return it


async def contact_overview(session: AsyncSession, contact_id: uuid.UUID) -> dict[str, Any]:
    c = await session.get(Contact, contact_id)
    if c is None:
        return {}
    interactions = (
        (
            await session.execute(
                select(CRMInteraction)
                .where(CRMInteraction.contact_id == contact_id)
                .order_by(CRMInteraction.created_at.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    tasks = (
        (
            await session.execute(
                select(Task).where(Task.assignee_contact_id == contact_id).order_by(Task.created_at.desc()).limit(30)
            )
        )
        .scalars()
        .all()
    )
    projects = (
        (await session.execute(select(Project).where(Project.member_ids.any(str(contact_id))))).scalars().all()
    )
    return {"contact": c, "interactions": list(interactions), "tasks": list(tasks), "projects": list(projects)}
