"""مرکز گفتگو: تاریخچه و جست‌وجو، ارسال پیام چندرسانه‌ای، پیوست‌ها، اجراها و کارت‌های مجوز."""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_agent.classifier import detect_command
from xagent_agent.commands import run_command
from xagent_agent.pipeline import load_attachments
from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.config import get_settings
from xagent_core.db import utcnow
from xagent_core.models import Approval, Conversation, Message, Run, RunStep, Upload, User
from xagent_core.text import normalize_fa, truncate

from ..deps import current_user, get_session, owner_user, parse_uuid, ser

router = APIRouter(prefix="/api", tags=["chat"])


def ser_approval(a: Approval, extra: dict | None = None) -> dict:
    from xagent_agent.harness import CATEGORIES
    from xagent_tools.registry import BUILTIN_BY_NAME

    tool = BUILTIN_BY_NAME.get(a.tool_name)
    return ser(a, extra={
        "category_label": CATEGORIES.get(a.category, {}).get("label", a.category),
        "tool_label": tool.label if tool else a.tool_name,
        **(extra or {}),
    })
MAX_UPLOAD = 50 * 1024 * 1024


class SendIn(BaseModel):
    conversation_id: str | None = None
    text: str = Field(default="", max_length=60000)
    attachments: list[str] = Field(default_factory=list)
    model: str = "auto"
    channel: str = "web"


class ConvPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    model_selection: str | None = None
    task_id: str | None = None


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")
    note: str | None = None


class ResumeIn(BaseModel):
    extra_budget_usd: float | None = Field(default=None, ge=0, le=100)
    extra_steps: int | None = Field(default=None, ge=0, le=200)


async def _publish_message(m: Message) -> None:
    await bus.publish("message.created", {
        "conversation_id": str(m.conversation_id), "id": str(m.id), "role": m.role, "content": m.content,
        "attachments": m.attachments, "run_id": str(m.run_id) if m.run_id else None, "meta": m.meta,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    })


# ── گفتگوها ──
@router.get("/conversations")
async def list_conversations(q: str | None = None, archived: bool = False, limit: int = Query(50, le=200),
                             offset: int = 0, _: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    stmt = select(Conversation).where(Conversation.archived.is_(archived))
    if q:
        nq = normalize_fa(q)
        sub = select(Message.conversation_id).where(
            Message.tsv.op("@@")(func.plainto_tsquery("simple", func.xa_normalize(q)))
        )
        stmt = stmt.where(or_(func.xa_normalize(Conversation.title).like(f"%{nq}%"), Conversation.id.in_(sub)))
    stmt = stmt.order_by(Conversation.pinned.desc(), Conversation.updated_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [ser(c) for c in rows]}


@router.post("/conversations")
async def create_conversation(body: ConvPatch, _: User = Depends(owner_user),
                              session: AsyncSession = Depends(get_session)):
    c = Conversation(title=body.title or "گفتگوی جدید", model_selection=body.model_selection or "auto")
    session.add(c)
    await session.flush()
    return ser(c)


@router.patch("/conversations/{cid}")
async def patch_conversation(cid: str, body: ConvPatch, _: User = Depends(owner_user),
                             session: AsyncSession = Depends(get_session)):
    c = await session.get(Conversation, parse_uuid(cid))
    if c is None:
        raise HTTPException(404, "گفتگو یافت نشد")
    for f in ("title", "pinned", "archived", "model_selection"):
        v = getattr(body, f)
        if v is not None:
            setattr(c, f, v)
    if body.task_id is not None:
        c.task_id = parse_uuid(body.task_id) if body.task_id else None
    return ser(c)


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, user: User = Depends(owner_user),
                              session: AsyncSession = Depends(get_session)):
    c = await session.get(Conversation, parse_uuid(cid))
    if c is None:
        raise HTTPException(404, "گفتگو یافت نشد")
    await session.execute(delete(Message).where(Message.conversation_id == c.id))
    await session.delete(c)
    await audit(session, user.username, "conversation.delete", str(c.id), {"title": c.title})
    return {"ok": True}


@router.get("/conversations/{cid}/messages")
async def list_messages(cid: str, before: str | None = None, limit: int = Query(60, le=200),
                        _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    conv = await session.get(Conversation, parse_uuid(cid))
    if conv is None:
        raise HTTPException(404, "گفتگو یافت نشد")
    stmt = select(Message).where(Message.conversation_id == conv.id)
    if before:
        ref = await session.get(Message, parse_uuid(before))
        if ref:
            stmt = stmt.where(Message.created_at < ref.created_at)
    rows = list((await session.execute(stmt.order_by(Message.created_at.desc()).limit(limit))).scalars().all())
    rows.reverse()
    run_ids = {m.run_id for m in rows if m.run_id}
    runs = {}
    if run_ids:
        for r in (await session.execute(select(Run).where(Run.id.in_(run_ids)))).scalars():
            runs[str(r.id)] = ser(r, exclude={"input", "result", "plan", "goal"})
    active = (
        await session.execute(
            select(Run).where(Run.conversation_id == conv.id,
                              Run.status.in_(["queued", "running", "waiting_approval", "paused"]))
            .order_by(Run.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    pending = []
    if active is not None:
        pending = [ser_approval(a) for a in (await session.execute(
            select(Approval).where(Approval.run_id == active.id, Approval.status == "pending"))).scalars()]
    return {
        "conversation": ser(conv),
        "items": [ser(m, exclude={"tsv"}) for m in rows],
        "runs": runs,
        "active_run": ser(active) if active else None,
        "pending_approvals": pending,
    }


# ── ارسال پیام ──
@router.post("/chat/send")
async def send(body: SendIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    text = body.text.strip()
    if not text and not body.attachments:
        raise HTTPException(400, "پیام خالی است")
    if body.conversation_id:
        conv = await session.get(Conversation, parse_uuid(body.conversation_id))
        if conv is None:
            raise HTTPException(404, "گفتگو یافت نشد")
    else:
        conv = Conversation(title=truncate(text.replace("\n", " "), 60) or "گفتگوی جدید",
                            channel=body.channel if body.channel in ("web", "voice") else "web",
                            model_selection=body.model)
        session.add(conv)
        await session.flush()
    attachments = await load_attachments(session, body.attachments)
    msg = Message(conversation_id=conv.id, role="user", content=text,
                  attachments=[{k: a[k] for k in ("id", "filename", "mime", "size")} for a in attachments],
                  meta={"channel": body.channel})
    session.add(msg)
    conv.message_count += 1
    conv.updated_at = utcnow()
    if body.model:
        conv.model_selection = body.model
    await session.flush()

    cmd = detect_command(text) if text and not attachments else None
    if cmd:
        # فرمان قطعی: فوری و بدون مدل (حتی اگر Worker مشغول باشد)
        reply = await run_command(session, cmd[0], cmd[1], actor=user.username)
        run = Run(conversation_id=conv.id, trigger="chat", status="succeeded", input=text, result=reply,
                  route={"path": "command", "command": cmd[0]}, started_at=utcnow(), finished_at=utcnow())
        session.add(run)
        await session.flush()
        msg.run_id = run.id
        answer = Message(conversation_id=conv.id, role="assistant", content=reply, run_id=run.id,
                         meta={"status": "succeeded", "route": "command"})
        session.add(answer)
        conv.message_count += 1
        await session.commit()
        await _publish_message(msg)
        await _publish_message(answer)
        return {"conversation": ser(conv), "message": ser(msg), "run": ser(run), "reply": ser(answer)}

    run = Run(conversation_id=conv.id, trigger="voice" if body.channel == "voice" else "chat", input=text,
              model_override=None if body.model == "auto" else body.model,
              state={"trusted": True, "principal": "owner", "requested_by": user.username, "attachments": attachments})
    session.add(run)
    await session.flush()
    msg.run_id = run.id
    await session.commit()
    await bus.enqueue_run(run.id)
    await _publish_message(msg)
    await bus.publish("run.status", {"run_id": str(run.id), "conversation_id": str(conv.id), "status": "queued"})
    return {"conversation": ser(conv), "message": ser(msg), "run": ser(run)}


# ── پیوست‌ها ──
@router.post("/uploads")
async def upload(file: UploadFile = File(...), conversation_id: str | None = None, _: User = Depends(owner_user),
                 session: AsyncSession = Depends(get_session)):
    s = get_settings()
    now = utcnow()
    folder = s.uploads_dir / f"{now:%Y}" / f"{now:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4()
    safe = "".join(ch for ch in (file.filename or "file") if ch.isalnum() or ch in "._- ")[:120] or "file"
    path = folder / f"{uid}_{safe}"
    h = hashlib.sha256()
    size = 0
    with path.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, "حجم فایل بیش از ۵۰ مگابایت است")
            h.update(chunk)
            f.write(chunk)
    mime = file.content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    u = Upload(id=uid, filename=file.filename or safe, mime=mime, size=size, path=str(path), sha256=h.hexdigest(),
               conversation_id=parse_uuid(conversation_id) if conversation_id else None)
    session.add(u)
    await session.flush()
    return ser(u, exclude={"path"})


@router.get("/uploads/{uid}")
async def get_upload(uid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    u = await session.get(Upload, parse_uuid(uid))
    if u is None or not Path(u.path).is_file():
        raise HTTPException(404, "فایل یافت نشد")
    return FileResponse(u.path, media_type=u.mime, filename=u.filename)


# ── اجراها ──
@router.get("/runs")
async def list_runs(status: str | None = None, limit: int = Query(50, le=200), _: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Run.status.in_(status.split(",")))
    return {"items": [ser(r, exclude={"plan", "goal"}) for r in (await session.execute(stmt)).scalars()]}


@router.get("/runs/{rid}")
async def get_run(rid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(Run, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "اجرا یافت نشد")
    steps = (await session.execute(select(RunStep).where(RunStep.run_id == r.id).order_by(RunStep.idx))).scalars()
    approvals = (await session.execute(select(Approval).where(Approval.run_id == r.id))).scalars()
    return {"run": ser(r), "steps": [ser(s) for s in steps], "approvals": [ser(a) for a in approvals]}


@router.post("/runs/{rid}/cancel")
async def cancel_run(rid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(Run, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "اجرا یافت نشد")
    await bus.request_run_cancel(r.id)
    if r.status in ("queued", "waiting_approval", "paused"):
        r.status = "cancelled"
        r.finished_at = utcnow()
        r.stop_reason = "cancelled"
        await session.execute(
            Approval.__table__.update().where(Approval.run_id == r.id, Approval.status == "pending")
            .values(status="expired")
        )
        if r.conversation_id:
            m = Message(conversation_id=r.conversation_id, role="assistant", content="⏹ اجرا توسط کارفرما لغو شد.",
                        run_id=r.id, meta={"status": "cancelled"})
            session.add(m)
            await session.flush()
            await _publish_message(m)
    await audit(session, user.username, "run.cancel", str(r.id))
    await bus.publish("run.status", {"run_id": str(r.id), "status": r.status,
                                     "conversation_id": str(r.conversation_id) if r.conversation_id else None})
    return ser(r, exclude={"plan", "goal"})


@router.post("/runs/{rid}/resume")
async def resume_run(rid: str, body: ResumeIn, user: User = Depends(owner_user),
                     session: AsyncSession = Depends(get_session)):
    r = await session.get(Run, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "اجرا یافت نشد")
    if r.status not in ("paused", "stopped", "failed"):
        raise HTTPException(400, "این اجرا قابل ادامه نیست")
    state = dict(r.state or {})
    if body.extra_budget_usd:
        state["max_cost"] = float(Decimal(r.cost_usd or 0)) + body.extra_budget_usd
    if body.extra_steps:
        state["max_steps"] = r.steps_count + body.extra_steps
    state["elapsed"] = 0
    r.state = state
    r.status = "queued"
    r.stop_reason = None
    r.finished_at = None
    await bus.clear_run_cancel(r.id)
    await audit(session, user.username, "run.resume", str(r.id), body.model_dump())
    await session.commit()
    await bus.enqueue_run(r.id, "resume")
    return {"ok": True}


# ── کارت‌های مجوز ──
@router.get("/approvals")
async def list_approvals(status: str = "pending", _: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Approval).where(Approval.status == status)
                                  .order_by(Approval.created_at.desc()).limit(100))).scalars().all()
    out = []
    for a in rows:
        run = await session.get(Run, a.run_id)
        out.append(ser_approval(a, extra={"conversation_id": run.conversation_id if run else None,
                                          "run_input": truncate(run.input, 200) if run else ""}))
    return {"items": out}


@router.post("/approvals/{aid}")
async def decide(aid: str, body: DecisionIn, user: User = Depends(owner_user),
                 session: AsyncSession = Depends(get_session)):
    a = await session.get(Approval, parse_uuid(aid))
    if a is None:
        raise HTTPException(404, "درخواست یافت نشد")
    if a.status != "pending":
        raise HTTPException(409, "قبلاً تصمیم‌گیری شده است")
    a.status = "approved" if body.decision == "approve" else "denied"
    a.decided_at = utcnow()
    a.decided_by = user.username
    a.decision_note = body.note
    await audit(session, user.username, f"approval.{a.status}", a.tool_name,
                {"category": a.category, "run_id": str(a.run_id)})
    run = await session.get(Run, a.run_id)
    remaining = (await session.execute(select(func.count()).select_from(Approval).where(
        Approval.run_id == a.run_id, Approval.status == "pending"))).scalar_one()
    if run is not None and run.status == "waiting_approval" and remaining == 0:
        run.status = "queued"
    await session.commit()
    await bus.publish("approval.decided", {"id": str(a.id), "run_id": str(a.run_id), "status": a.status,
                                           "conversation_id": str(run.conversation_id) if run and run.conversation_id else None})
    if run is not None and remaining == 0:
        await bus.enqueue_run(run.id, "approval")
    return ser(a)
