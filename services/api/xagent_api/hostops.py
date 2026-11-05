"""لایه سیاست و حسابرسی کارگزار فرمان میزبان (فقط در API اجرا می‌شود).

- تنها نقطه اتصال به xagent-hostd (سوکت یونیکس فقط‌خواندنی در کانتینر API)
- تصمیم: سطح عملیات (از خود hostd)، سیاست کارفرما (HostSettings)، سقف تعداد سطح ۲، کارت اجازه سطح ۳
- مجوز سطح ۳ (grant) فقط پس از تأیید کارفرما امضا می‌شود؛ یک‌بارمصرف و با زمان انقضا (hostd دوباره بررسی می‌کند)
- هر درخواست در جدول host_ops ثبت می‌شود: درخواست‌کننده، زمان، مجوز، خروجی پاک‌سازی‌شده، کد خروج و نتیجه
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Approval, HostOp, User
from xagent_core.redact import scrub
from xagent_core.settings_store import HostSettings, get_setting
from xagent_core.timeutil import get_zone

TERMINAL = {"succeeded", "failed", "timeout", "cancelled", "lost"}
OUTPUT_KEEP = 64 * 1024

# فرم آرگومان‌ها برای پنل (اعتبارسنجی قطعی در hostd انجام می‌شود)
SERVICES_APP = ["api", "worker", "web", "runner", "desktop"]
SERVICES_ALL = SERVICES_APP + ["postgres", "redis"]
ARG_FORMS: dict[str, dict[str, dict[str, Any]]] = {
    "logs.service": {"service": {"type": "enum", "choices": SERVICES_ALL}, "lines": {"type": "int", "min": 1, "max": 1000, "default": 200}},
    "logs.unit": {"unit": {"type": "enum", "choices": ["docker", "containerd", "xagent-hostd", "xagent-updater", "ssh", "cron"]},
                  "lines": {"type": "int", "min": 1, "max": 1000, "default": 200}},
    "update.check": {"ref": {"type": "text", "optional": True, "placeholder": "شاخه/تگ (اختیاری)"}},
    "diag.dns": {"host": {"type": "text", "placeholder": "example.com"}},
    "diag.ping": {"host": {"type": "text", "placeholder": "example.com"}},
    "diag.http": {"url": {"type": "text", "placeholder": "https://example.com"}},
    "service.restart": {"service": {"type": "enum", "choices": SERVICES_APP}},
    "service.restart_data": {"service": {"type": "enum", "choices": ["postgres", "redis"]}},
    "xagent.update": {"ref": {"type": "text", "optional": True, "placeholder": "شاخه/تگ (اختیاری)"}},
    "system.apt_install": {"packages": {"type": "list", "placeholder": "htop, ncdu"}},
    "firewall.allow": {"port": {"type": "int", "min": 1, "max": 65535}, "proto": {"type": "enum", "choices": ["tcp", "udp"]}},
    "firewall.deny": {"port": {"type": "int", "min": 1, "max": 65535}, "proto": {"type": "enum", "choices": ["tcp", "udp"]}},
    "exec.approved": {"argv": {"type": "list", "placeholder": "/usr/bin/ls, -la, /opt"},
                      "run_as": {"type": "enum", "choices": ["nobody", "root"], "default": "nobody"},
                      "timeout": {"type": "int", "min": 1, "max": 600, "default": 120}},
}
# این عملیات حتی با سیاست کارفرما هرگز خودکار نمی‌شوند
HARD_APPROVAL = {"exec.approved", "system.apt_install", "system.security_updates", "firewall.allow", "firewall.deny",
                 "system.reboot", "xagent.rollback", "service.restart_data", "stack.restart"}


class HostUnavailable(Exception):
    pass


class HostDenied(Exception):
    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.code = code


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _derive(label: bytes) -> bytes:
    return hmac.new(get_settings().secret_key.encode(), label, hashlib.sha256).digest()


def internal_token() -> str:
    """توکن داخلی Worker→API (مشتق از کلید اصلی؛ هرگز ذخیره یا نمایش داده نمی‌شود)."""
    return _derive(b"xagent-internal-v1").hex()


def sign_grant(op: str, args: dict, *, minutes: int, approval_id: str | None, by: str) -> dict:
    g = {"op": op, "args_hash": hashlib.sha256(canonical(args)).hexdigest(), "exp": int(time.time()) + minutes * 60,
         "nonce": secrets.token_hex(16), "approval_id": approval_id, "by": by}
    g["mac"] = hmac.new(_derive(b"xagent-hostd-grant-v1"), canonical({k: v for k, v in g.items()}), hashlib.sha256).hexdigest()
    return g


async def hostd_call(op: str, args: dict | None = None, *, actor: str = "", timeout: float = 15.0) -> dict:
    req = {"v": 1, "op": op, "args": args or {}, "ts": int(time.time()), "nonce": secrets.token_hex(16), "actor": actor}
    req["mac"] = hmac.new(_derive(b"xagent-hostd-request-v1"), canonical(req), hashlib.sha256).hexdigest()
    path = get_settings().hostd_socket
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), 5)
    except (OSError, TimeoutError) as e:
        raise HostUnavailable("سرویس میزبان (xagent-hostd) در دسترس نیست؛ وضعیت آن را با «sudo systemctl status xagent-hostd.socket» بررسی کنید.") from e
    try:
        writer.write(json.dumps(req, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, TimeoutError) as e:
        raise HostUnavailable("پاسخ سرویس میزبان دریافت نشد.") from e
    finally:
        writer.close()
    if not line:
        raise HostUnavailable("سرویس میزبان اتصال را بست.")
    return json.loads(line)


_catalog_cache: tuple[float, dict[str, dict]] = (0.0, {})


async def catalog() -> dict[str, dict]:
    global _catalog_cache
    if time.monotonic() - _catalog_cache[0] < 60 and _catalog_cache[1]:
        return _catalog_cache[1]
    r = await hostd_call("catalog")
    if not r.get("ok"):
        raise HostUnavailable(r.get("error") or "فهرست عملیات دریافت نشد")
    cat = {o["op"]: {**o, "form": ARG_FORMS.get(o["op"], {})} for o in r["ops"]}
    _catalog_cache = (time.monotonic(), cat)
    return cat


def _in_window(hs: HostSettings) -> bool:
    from xagent_core.settings_store import GeneralSettings  # noqa: F401  (منطقه زمانی در نسخه بعد)

    hour = datetime.now(get_zone("Asia/Tehran")).hour
    a, b = hs.auto_window_start_hour, hs.auto_window_end_hour
    return a <= hour < b if a <= b else (hour >= a or hour < b)


async def _recent_count(session: AsyncSession, op: str, args: dict, since: timedelta) -> int:
    q = select(func.count()).select_from(HostOp).where(
        HostOp.op == op, HostOp.created_at >= utcnow() - since,
        HostOp.status.in_(["running", "succeeded", "failed", "timeout", "queued"]))
    if "service" in args:
        q = q.where(HostOp.args["service"].astext == str(args["service"]))
    return int((await session.execute(q)).scalar_one())


async def decide(session: AsyncSession, op: str, args: dict, actor_kind: str) -> tuple[str, int, str]:
    """خروجی: (run | approval | auto_grant, سطح، توضیح) یا HostDenied."""
    cat = await catalog()
    meta = cat.get(op)
    if meta is None:
        raise HostDenied(f"عملیات «{op}» در فهرست مجاز نیست؛ برای فرمان خاص از exec.approved با تأیید کارفرما استفاده کن.",
                         "not_allowed")
    hs = await get_setting(session, "host", HostSettings)
    if not hs.enabled:
        raise HostDenied("کارگزار میزبان توسط کارفرما غیرفعال شده است.", "disabled")
    level = int(meta["level"])
    mode = hs.op_modes.get(op)
    if mode == "forbidden":
        raise HostDenied(f"کارفرما عملیات «{op}» را ممنوع کرده است.", "forbidden")
    if actor_kind == "owner":
        return "run", level, "اجرای مستقیم کارفرما"
    if level >= 3 or op in HARD_APPROVAL:
        if op == "xagent.update" and hs.update_policy == "auto" and _in_window(hs) and not args.get("ref"):
            return "auto_grant", level, "سیاست به‌روزرسانی خودکار کارفرما (در بازه مجاز)"
        return "approval", level, "عملیات سطح ۳ فقط با تأیید کارفرما"
    if mode == "ask":
        return "approval", level, "کارفرما برای این عملیات تأیید خواسته است"
    if level == 2:
        limits = {"service.restart": (hs.restart_per_hour, timedelta(hours=1)),
                  "xagent.backup": (hs.backup_per_hour, timedelta(hours=1)),
                  "docker.prune": (hs.prune_per_day, timedelta(days=1)),
                  "logs.vacuum": (hs.prune_per_day, timedelta(days=1))}
        cap, window = limits.get(op, (2, timedelta(hours=1)))
        if await _recent_count(session, op, args, window) >= cap:
            raise HostDenied(f"سقف تعداد اجرای خودکار «{op}» ({cap} بار در {'ساعت' if window.days == 0 else 'روز'}) پر شده است؛ "
                             "برای اجرای بیشتر از کارفرما تأیید بگیر.", "rate_limited")
    return "run", level, "مجاز طبق سطح و سیاست"


async def verify_approval(session: AsyncSession, run_id: str | None, tool_call_id: str | None, op: str, args: dict) -> Approval:
    """کارت اجازه باید: متعلق به همین اجرا و فراخوانی، دسته host_admin، تأییدشده توسط کارفرما، تازه، یک‌بارمصرف و هم‌خوان باشد."""
    try:
        rid = uuid.UUID(str(run_id))
    except ValueError as e:
        raise HostDenied("شناسه اجرا نامعتبر است.", "approval_invalid") from e
    ap = (await session.execute(select(Approval).where(
        Approval.run_id == rid, Approval.tool_call_id == str(tool_call_id or ""), Approval.category == "host_admin")
        .order_by(Approval.created_at.desc()))).scalars().first()
    if ap is None or ap.status != "approved":
        raise HostDenied("کارت اجازه تأییدشده‌ای برای این عملیات وجود ندارد.", "approval_required")
    owner = (await session.execute(select(User).where(User.username == ap.decided_by))).scalar_one_or_none()
    if owner is None or owner.role != "owner":
        raise HostDenied("کارت اجازه توسط کارفرما تأیید نشده است.", "approval_invalid")
    hs = await get_setting(session, "host", HostSettings)
    decided = ap.decided_at or ap.created_at
    if decided is None or utcnow() - decided > timedelta(minutes=max(hs.exec_session_minutes, 15) if op != "exec.approved"
                                                         else hs.exec_session_minutes):
        raise HostDenied("اعتبار زمانی کارت اجازه تمام شده است؛ دوباره درخواست بده.", "approval_expired")
    a_args = ap.args or {}
    if a_args.get("op") != op or canonical(a_args.get("args") or {}) != canonical(args):
        raise HostDenied("عملیات درخواستی با آنچه کارفرما تأیید کرده یکسان نیست.", "approval_mismatch")
    used = (await session.execute(select(HostOp.id).where(HostOp.approval_id == ap.id))).first()
    if used is not None:
        raise HostDenied("این کارت اجازه قبلاً استفاده شده است (یک‌بارمصرف).", "approval_used")
    return ap


async def submit(session: AsyncSession, *, op: str, args: dict, level: int, actor_kind: str, requested_by: str,
                 reason: str = "", run_id: str | None = None, approval: Approval | None = None,
                 approved_by: str | None = None, grant_minutes: int = 15) -> HostOp:
    row = HostOp(op=op, args=args, level=level, requested_by=requested_by[:64], actor_kind=actor_kind, reason=reason[:2000],
                 run_id=uuid.UUID(run_id) if run_id else None, approval_id=approval.id if approval else None,
                 approved_by=approved_by, status="queued", output="", output_bytes=0, truncated=False, duration_ms=0)
    session.add(row)
    await session.flush()
    grant = None
    if level >= 3:
        grant = sign_grant(op, args, minutes=grant_minutes, approval_id=str(approval.id) if approval else None,
                           by=approved_by or requested_by)
    try:
        resp = await hostd_call("run", {"op": op, "args": args, "grant": grant}, actor=f"{actor_kind}:{requested_by}")
    except HostUnavailable as e:
        row.status, row.error, row.finished_at = "failed", str(e), utcnow()
        await audit(session, requested_by, "host.unavailable", op, {"args": args})
        return row
    if not resp.get("ok"):
        code = resp.get("code") or "rejected"
        row.status = "rejected" if code != "busy" else "failed"
        row.error = scrub(("سرویس میزبان مشغول عملیات دیگری است (قفل عملیاتی): " if code == "busy" else "رد شد: ")
                          + str(resp.get("error")))
        row.finished_at = utcnow()
        await audit(session, requested_by, f"host.{code}", op, {"args": args})
        return row
    row.host_job, row.status, row.started_at = resp["job"], "running", utcnow()
    await audit(session, requested_by, "host.started", op, {"args": args, "level": level, "job": resp["job"],
                                                            "approved_by": approved_by})
    await bus.publish("host.op", {"id": str(row.id), "op": op, "status": "running"})
    return row


def _jobs_dir() -> str:
    return get_settings().hostd_jobs_dir


def read_job(job: str) -> dict | None:
    if not job or not all(c in "0123456789abcdef" for c in job):
        return None
    try:
        with open(os.path.join(_jobs_dir(), f"{job}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_output(job: str, offset: int = 0, limit: int = 65536) -> tuple[str, int]:
    if not job or not all(c in "0123456789abcdef" for c in job):
        return "", offset
    try:
        with open(os.path.join(_jobs_dir(), f"{job}.out"), "rb") as f:
            f.seek(max(0, offset))
            data = f.read(limit)
    except OSError:
        return "", offset
    return scrub(data.decode("utf-8", "replace")), offset + len(data)


def _tail(job: str) -> tuple[str, int]:
    try:
        size = os.path.getsize(os.path.join(_jobs_dir(), f"{job}.out"))
    except OSError:
        return "", 0
    text, _ = read_output(job, max(0, size - OUTPUT_KEEP), OUTPUT_KEEP)
    return text, size


async def sync(session: AsyncSession, row: HostOp) -> HostOp:
    """هم‌گام‌سازی وضعیت کار از پوشه کارهای میزبان؛ اگر کار پایان یافته باشد خروجی و نتیجه ثبت می‌شود."""
    if row.status != "running" or not row.host_job:
        return row
    st = read_job(row.host_job)
    if st is not None and st.get("status") in ("running", "starting"):
        started = row.started_at or row.created_at
        if started and utcnow() - started > timedelta(seconds=int(st.get("timeout", 600)) + 120):
            try:  # از hostd بخواه وضعیت فرایندهای مرده را بازبینی کند
                await hostd_call("job.status", {"job": row.host_job})
            except HostUnavailable:
                pass
            st = read_job(row.host_job)
    if st is None or st.get("status") not in TERMINAL:
        row.output, row.output_bytes = _tail(row.host_job)
        return row
    row.status = st["status"]
    row.exit_code = st.get("exit_code")
    row.output, row.output_bytes = _tail(row.host_job)
    row.truncated = bool(st.get("truncated"))
    row.error = scrub(st.get("error")) if st.get("error") else None
    row.finished_at = utcnow()
    if st.get("started_at") and st.get("finished_at"):
        row.duration_ms = int((float(st["finished_at"]) - float(st["started_at"])) * 1000)
    await audit(session, row.requested_by, f"host.{row.status}", row.op, {"exit_code": row.exit_code, "job": row.host_job})
    await bus.publish("host.op", {"id": str(row.id), "op": row.op, "status": row.status})
    return row


async def wait_done(session: AsyncSession, row: HostOp, seconds: float) -> HostOp:
    end = time.monotonic() + seconds
    while row.status == "running" and time.monotonic() < end:
        await asyncio.sleep(0.5)
        await sync(session, row)
    return row


async def reconcile_loop() -> None:
    """پایش پس‌زمینه کارهای در حال اجرا (حتی اگر API در حین کار ری‌استارت شده باشد)."""
    while True:
        try:
            async with session_scope() as s:
                rows = (await s.execute(select(HostOp).where(HostOp.status == "running").limit(50))).scalars().all()
                for r in rows:
                    await sync(s, r)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3)


def ser_op(r: HostOp) -> dict[str, Any]:
    return {"id": str(r.id), "op": r.op, "args": r.args, "level": r.level, "status": r.status,
            "requested_by": r.requested_by, "actor_kind": r.actor_kind, "reason": r.reason,
            "approved_by": r.approved_by, "exit_code": r.exit_code, "output": scrub(r.output or ""),
            "output_bytes": r.output_bytes, "truncated": r.truncated, "duration_ms": r.duration_ms,
            "error": scrub(r.error) if r.error else None, "job": r.host_job,
            "run_id": str(r.run_id) if r.run_id else None, "approval_id": str(r.approval_id) if r.approval_id else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


_ = UTC
