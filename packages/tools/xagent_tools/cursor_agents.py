"""اجراکننده ایجنت بیرونی Cursor Cloud Agents — یک «External Agent Executor»، نه LLM با Chat Completions.

مستندات قرارداد: https://cursor.com/docs/cloud-agent/api/endpoints
- احراز هویت: Basic (کلید به‌عنوان نام کاربری، رمز خالی). کلید فقط به api.cursor.com ارسال می‌شود.
- ایجاد: POST /v1/agents با agentId ساخته‌شده توسط کلاینت (bc-<uuid>)؛ ارسال تکراری → 409 agent_id_conflict.
  بنابراین هر تلاش مجدد (قطعی شبکه، Timeout، ری‌استارت Worker) همان Agent را بازمی‌یابد و Agent هزینه‌زای
  تکراری ساخته نمی‌شود. ادعای Exactly-once نمی‌کنیم؛ تضمین فقط تا حد رفتار مستند agentId است.
- پایش: GET /v1/agents/{id}/runs/{runId} توسط زمان‌بند Worker و بدون هیچ فراخوانی LLM.
- لغو: POST /v1/agents/{id}/runs/{runId}/cancel؛ 409 run_not_cancellable یعنی اجرا پایان یافته است.

ایمنی: فقط مخازن مجاز؛ workOnCurrentBranch همیشه false (کار روی شاخه تازه، نه شاخه مبدأ/main)؛ autoCreatePR طبق
تنظیم کارفرما (پیش‌فرض خاموش)؛ Merge خودکار وجود ندارد؛ envVars/mcpServers/فایل‌ها ارسال نمی‌شوند؛ متن کار پیش از
ارسال از نظر اسرار بررسی و در صورت وجود رد می‌شود. هزینه Cursor از API مستند استخراج‌پذیر نیست → «نامعلوم».
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus, inbox
from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import ExternalAgentRun, Run, SettingKV
from xagent_core.redact import scrub
from xagent_core.security import decrypt_secret, encrypt_secret, mask_secret
from xagent_core.settings_store import CursorSettings, get_setting
from xagent_core.text import truncate

log = logging.getLogger("xagent.cursor")

SECRET_KEY = "cursor_secret"
ACTIVE = ("reserved", "creating", "running", "unknown")
TERMINAL = {"FINISHED": "finished", "ERROR": "error", "CANCELLED": "cancelled", "EXPIRED": "expired"}
STATUS_FA = {"reserved": "در صف ایجاد", "creating": "در حال ایجاد", "running": "در حال اجرا",
             "unknown": "نتیجه ایجاد نامعلوم (در حال تطبیق)", "finished": "پایان‌یافته", "error": "خطا",
             "cancelled": "لغوشده", "expired": "منقضی", "rejected": "رد شد (ارسال نشد)"}


class CursorError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None,
                 definitely_not_created: bool = False) -> None:
        super().__init__(scrub(message))
        self.status, self.code = status, code
        self.definitely_not_created = definitely_not_created

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status in (429, 500, 502, 503, 504)


# ───────────── پیکربندی و کلید ─────────────
async def get_key(session: AsyncSession) -> str | None:
    row = await session.get(SettingKV, SECRET_KEY)
    if row is None or not (row.value or {}).get("api_key"):
        return None
    try:
        return decrypt_secret(base64.b64decode(row.value["api_key"]))
    except Exception:  # noqa: BLE001
        return None


async def set_key(session: AsyncSession, key: str | None) -> None:
    row = await session.get(SettingKV, SECRET_KEY)
    value = {"api_key": base64.b64encode(encrypt_secret(key)).decode()} if key else {}
    if row is None:
        session.add(SettingKV(key=SECRET_KEY, value=value))
    else:
        row.value = value
    await session.flush()


def masked(key: str | None) -> str | None:
    return mask_secret(key) if key else None


def validate_base_url(url: str) -> str:
    """کلید Cursor فقط به api.cursor.com می‌رود؛ آدرس محلی فقط در محیط آزمون/توسعه (Fake API)."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if u.scheme == "https" and host == "api.cursor.com" and not u.username and not u.password:
        return url.rstrip("/")
    if get_settings().env != "production" and u.scheme in ("http", "https") and host in ("127.0.0.1", "localhost"):
        return url.rstrip("/")
    raise CursorError("آدرس API نامعتبر است؛ فقط https://api.cursor.com مجاز است.")


def norm_repo(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", u):
        u = "https://github.com/" + u
    return u


async def config(session: AsyncSession) -> tuple[CursorSettings, str | None]:
    return await get_setting(session, "cursor", CursorSettings), await get_key(session)


async def readiness(session: AsyncSession) -> tuple[str, str]:
    cfg, key = await config(session)
    if not cfg.enabled:
        return "needs_setup", "اجراکننده Cursor غیرفعال است"
    if not key:
        return "needs_setup", "کلید API کرسر ثبت نشده است"
    if not cfg.allowed_repos:
        return "needs_setup", "هیچ مخزن مجازی تعریف نشده است"
    st = (await session.get(SettingKV, "cursor_status"))
    v = (st.value if st else {}) or {}
    if v.get("status") in ("ready", "needs_setup", "temporarily_unavailable"):
        return v["status"], v.get("reason", "")
    return "untested", "اتصال هنوز آزموده نشده است"


# ───────────── کلاینت HTTP ─────────────
class CursorClient:
    def __init__(self, key: str, base_url: str = "https://api.cursor.com", timeout: float = 30) -> None:
        self.base = validate_base_url(base_url)
        self._c = httpx.AsyncClient(base_url=self.base, auth=httpx.BasicAuth(key, ""), timeout=timeout,
                                    follow_redirects=False, headers={"Accept": "application/json"})

    async def aclose(self) -> None:
        await self._c.aclose()

    async def _req(self, method: str, path: str, json: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            r = await self._c.request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            raise CursorError(f"ارتباط با Cursor ناموفق بود: {type(e).__name__}") from e
        if r.status_code >= 400:
            code = None
            msg = ""
            try:
                err = (r.json() or {}).get("error") or {}
                code, msg = err.get("code"), err.get("message") or ""
            except ValueError:
                pass
            raise CursorError(f"Cursor HTTP {r.status_code} {code or ''} {msg[:200]}".strip(), r.status_code, code,
                              definitely_not_created=r.status_code in (400, 401, 403, 422))
        try:
            return r.json()
        except ValueError as e:
            raise CursorError("پاسخ Cursor JSON نیست") from e

    async def me(self) -> dict[str, Any]:
        return await self._req("GET", "/v1/me")

    async def models(self) -> list[dict[str, Any]]:
        return (await self._req("GET", "/v1/models")).get("items") or []

    async def create_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._req("POST", "/v1/agents", json=body)

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/v1/agents/{agent_id}")

    async def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/v1/agents/{agent_id}/runs/{run_id}")

    async def cancel_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return await self._req("POST", f"/v1/agents/{agent_id}/runs/{run_id}/cancel")


async def _client(session: AsyncSession) -> CursorClient:
    cfg, key = await config(session)
    if not key:
        raise CursorError("کلید API کرسر ثبت نشده است", definitely_not_created=True)
    return CursorClient(key, cfg.base_url)


async def test_connection(session: AsyncSession) -> dict[str, Any]:
    """آزمون اتصال با ‎/v1/me و ‎/v1/models (بدون ساخت Agent و بدون هزینه)."""
    try:
        c = await _client(session)
    except CursorError as e:
        status = {"status": "needs_setup", "reason": str(e)}
        await _save_status(session, status)
        return status
    try:
        me = await c.me()
        models = await c.models()
        status = {"status": "ready", "reason": "کلید معتبر است", "api_key_name": me.get("apiKeyName"),
                  "models": [m.get("id") for m in models if isinstance(m, dict) and m.get("id")][:50],
                  "checked_at": utcnow().isoformat()}
    except CursorError as e:
        kind = "needs_setup" if e.status in (401, 403) else "temporarily_unavailable"
        status = {"status": kind, "reason": str(e)[:300], "checked_at": utcnow().isoformat()}
    finally:
        await c.aclose()
    await _save_status(session, status)
    return status


async def _save_status(session: AsyncSession, status: dict[str, Any]) -> None:
    row = await session.get(SettingKV, "cursor_status")
    if row is None:
        session.add(SettingKV(key="cursor_status", value=status))
    else:
        row.value = status
    await session.flush()


# ───────────── واگذاری کار ─────────────
@dataclass
class LaunchResult:
    ok: bool
    message: str
    row_id: uuid.UUID | None = None
    status: str | None = None
    duplicate: bool = False


def _check_prompt(text: str) -> str | None:
    if scrub(text) != text:
        return "متن کار شامل کلید/توکن یا اطلاعات محرمانه است؛ برای ارسال به سرویس بیرونی مجاز نیست."
    if re.search(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bid_rsa\b|\.env\b)", text):
        return "متن کار به فایل خصوصی (کلید/‎.env) اشاره دارد؛ اسرار و فایل‌های خصوصی به Cursor ارسال نمی‌شوند."
    return None


async def launch(session: AsyncSession, *, dedupe_key: str, repo: str, task: str, title: str,
                 starting_ref: str | None, requested_by: str, run_id: uuid.UUID | None = None,
                 task_id: uuid.UUID | None = None) -> LaunchResult:
    cfg, key = await config(session)
    if not cfg.enabled or not key:
        return LaunchResult(False, "اجراکننده Cursor فعال یا تنظیم نشده است (Needs Setup).")
    if await bus.is_stop_all():
        return LaunchResult(False, "توقف اضطراری فعال است؛ کار بیرونی جدید ساخته نمی‌شود.")
    repo_n = norm_repo(repo)
    allowed = {norm_repo(r) for r in cfg.allowed_repos}
    if repo_n not in allowed:
        return LaunchResult(False, f"مخزن «{repo}» در فهرست مجاز Cursor نیست. فقط کارفرما می‌تواند آن را اضافه کند.")
    ref = (starting_ref or "").strip() or None
    if ref and cfg.allowed_refs and ref not in cfg.allowed_refs:
        return LaunchResult(False, f"شاخه مبدأ «{ref}» مجاز نیست.")
    if cfg.allowed_refs and not ref:
        ref = cfg.allowed_refs[0]
    problem = _check_prompt(task) or _check_prompt(title)
    if problem:
        return LaunchResult(False, problem)
    existing = (await session.execute(select(ExternalAgentRun).where(
        ExternalAgentRun.dedupe_key == dedupe_key))).scalar_one_or_none()
    if existing is not None:
        return LaunchResult(True, f"این کار قبلاً واگذار شده است ({STATUS_FA.get(existing.status, existing.status)}).",
                            existing.id, existing.status, duplicate=True)
    active = (await session.execute(select(func.count()).select_from(ExternalAgentRun).where(
        ExternalAgentRun.status.in_(ACTIVE)))).scalar_one()
    if active >= cfg.max_concurrent:
        return LaunchResult(False, f"سقف اجرای هم‌زمان Cursor ({cfg.max_concurrent}) پر است؛ پس از پایان کار فعلی.")
    today = (await session.execute(select(func.count()).select_from(ExternalAgentRun).where(
        ExternalAgentRun.created_at >= utcnow() - timedelta(hours=24),
        ExternalAgentRun.status != "rejected"))).scalar_one()
    if today >= cfg.max_launches_per_day:
        return LaunchResult(False, f"سقف روزانه واگذاری به Cursor ({cfg.max_launches_per_day}) پر شده است.")
    # رزرو پایدار پیش از هر تماس شبکه (تراکنش مستقل): پس از Crash، زمان‌بند از روی همین ردیف تطبیق می‌دهد
    rid = uuid.uuid4()
    async with session_scope() as s:
        res = await s.execute(insert(ExternalAgentRun).values(
            id=rid, created_at=utcnow(), updated_at=utcnow(), executor="cursor", run_id=run_id, task_id=task_id,
            dedupe_key=dedupe_key, agent_id=f"bc-{uuid.uuid4()}", status="reserved", repo_url=repo_n,
            starting_ref=ref, title=truncate(title, 190), prompt=task, model=cfg.model or None,
            auto_create_pr=cfg.auto_create_pr, requested_by=requested_by, cancel_requested=False, poll_count=0,
            cost_status="unknown", detail={},
        ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(ExternalAgentRun.id))
        if res.scalar_one_or_none() is None:
            return LaunchResult(True, "این کار هم‌زمان توسط فرایند دیگری واگذار شد.", None, None, duplicate=True)
    async with session_scope() as s:
        row = await s.get(ExternalAgentRun, rid)
        await _create_remote(s, row)
        status, err, url = row.status, row.error, row.agent_url
    if status == "rejected":
        return LaunchResult(False, f"Cursor درخواست را رد کرد: {err}", rid, status)
    if status == "unknown":
        return LaunchResult(True, "درخواست ایجاد ارسال شد ولی پاسخ نامعلوم است؛ زمان‌بند با همان شناسه تطبیق می‌دهد "
                                  "(Agent تکراری ساخته نمی‌شود).", rid, status)
    return LaunchResult(True, f"کار به Cursor Cloud Agent واگذار شد و در پس‌زمینه پایش می‌شود. {url or ''}".strip(),
                        rid, status)


def _body(row: ExternalAgentRun) -> dict[str, Any]:
    repo: dict[str, Any] = {"url": row.repo_url}
    if row.starting_ref:
        repo["startingRef"] = row.starting_ref
    body: dict[str, Any] = {
        "agentId": row.agent_id,
        "name": row.title[:120],
        "prompt": {"text": row.prompt},
        "repos": [repo],
        "workOnCurrentBranch": False,  # هرگز مستقیم روی شاخه مبدأ (مثلاً main) کار نمی‌کند
        "autoCreatePR": bool(row.auto_create_pr),
        "mode": "agent",
    }
    if row.model:
        body["model"] = {"id": row.model}
    return body


def _adopt(row: ExternalAgentRun, agent: dict[str, Any], run: dict[str, Any] | None = None) -> None:
    row.remote_run_id = (run or {}).get("id") or agent.get("latestRunId") or row.remote_run_id
    row.agent_url = agent.get("url") or row.agent_url
    row.remote_status = (run or {}).get("status") or row.remote_status or "CREATING"
    row.status = "running"
    row.error = None


async def _create_remote(s: AsyncSession, row: ExternalAgentRun) -> None:
    cfg = await get_setting(s, "cursor", CursorSettings)
    row.status, row.updated_at = "creating", utcnow()
    await s.flush()
    try:
        c = await _client(s)
    except CursorError as e:
        row.status, row.error = "rejected", str(e)
        return
    try:
        try:
            data = await c.create_agent(_body(row))
            _adopt(row, data.get("agent") or {}, data.get("run") or {})
        except CursorError as e:
            if e.status == 409 and e.code == "agent_id_conflict":
                _adopt(row, await c.get_agent(row.agent_id))  # قبلاً ساخته شده: همان را بازیابی کن
            elif e.definitely_not_created:
                row.status, row.error = "rejected", str(e)
            else:
                row.status, row.error = "unknown", str(e)
    except CursorError as e:
        row.status, row.error = "unknown", str(e)
    finally:
        await c.aclose()
    row.next_poll_at = utcnow() + timedelta(seconds=cfg.poll_seconds if row.status == "running" else 30)
    row.updated_at = utcnow()


async def request_cancel(session: AsyncSession, row: ExternalAgentRun, actor: str) -> str:
    if row.status not in ACTIVE:
        return f"اجرا در وضعیت «{STATUS_FA.get(row.status, row.status)}» است و لغو لازم ندارد."
    row.cancel_requested = True
    row.next_poll_at = utcnow()
    row.detail = {**(row.detail or {}), "cancel_by": actor, "cancel_at": utcnow().isoformat()}
    await session.flush()
    return "درخواست لغو ثبت شد؛ زمان‌بند آن را به Cursor اعمال می‌کند."


# ───────────── پایش زمان‌بند (بدون LLM) ─────────────
async def _finalize(s: AsyncSession, row: ExternalAgentRun, run: dict[str, Any] | None, status: str,
                    note: str | None = None) -> None:
    row.status = status
    row.finished_at = utcnow()
    row.next_poll_at = None
    if run:
        branches = (run.get("git") or {}).get("branches") or []
        if branches:
            b = branches[0] or {}
            row.branch = b.get("branch") or row.branch
            row.pr_url = b.get("prUrl") or row.pr_url
        if run.get("result"):
            row.result_summary = truncate(scrub(str(run["result"])), 4000)
        row.detail = {**(row.detail or {}), "duration_ms": run.get("durationMs"), "remote_status": run.get("status")}
    if note:
        row.detail = {**(row.detail or {}), "note": note}
    lines = [f"مخزن: {row.repo_url}", f"وضعیت: {STATUS_FA.get(status, status)}"]
    if row.branch:
        lines.append(f"شاخه: {row.branch}")
    if row.pr_url:
        lines.append(f"Pull Request (بدون Merge خودکار؛ بررسی و Merge با شما): {row.pr_url}")
    if row.result_summary:
        lines.append("نتیجه اعلام‌شده توسط Cursor (تأییدنشده تا بررسی شما):\n" + row.result_summary)
    if note:
        lines.append(note)
    lines.append("هزینه: نامعلوم — API مستند Cursor هزینه این اجرا را برنمی‌گرداند (در دفتر مصرف عددی ثبت نشد).")
    await inbox.post(s, kind="report", severity="important" if status == "finished" else "critical"
                     if status == "error" else "info",
                     title=f"Cursor: {row.title} — {STATUS_FA.get(status, status)}", body="\n".join(lines),
                     run_id=row.run_id, link=row.pr_url or row.agent_url, dedupe_key=f"cursor-final:{row.id}",
                     data={"external_agent_run_id": str(row.id), "agent_id": row.agent_id})


async def _refresh(s: AsyncSession, c: CursorClient, row: ExternalAgentRun, cfg: CursorSettings) -> None:
    now = utcnow()
    row.poll_count += 1
    row.updated_at = now
    # انتشار لغو: توقف اضطراری، لغو اجرای والد، یا عبور از سقف زمان اجرا
    if await bus.is_stop_all():
        row.cancel_requested = True
    if row.run_id and not row.cancel_requested:
        parent = await s.get(Run, row.run_id)
        if parent is not None and parent.status in ("cancelled",):
            row.cancel_requested = True
    if row.created_at and now - row.created_at > timedelta(hours=cfg.max_runtime_hours):
        row.cancel_requested = True
        row.detail = {**(row.detail or {}), "timeout": True}
    try:
        if row.status in ("reserved", "creating", "unknown"):
            try:
                agent = await c.get_agent(row.agent_id)
                _adopt(row, agent)
            except CursorError as e:
                if e.status != 404:
                    raise
                if row.cancel_requested:
                    await _finalize(s, row, None, "cancelled", "پیش از ایجاد در Cursor لغو شد (هیچ Agentی ساخته نشد).")
                    return
                await _create_remote(s, row)  # همان agentId: تکرار ایمن
                return
        if row.status == "running" and row.remote_run_id:
            if row.cancel_requested:
                try:
                    await c.cancel_run(row.agent_id, row.remote_run_id)
                except CursorError as e:
                    if not (e.status == 409 and e.code == "run_not_cancellable"):
                        raise
            run = await c.get_run(row.agent_id, row.remote_run_id)
            row.remote_status = run.get("status")
            if row.remote_status in TERMINAL:
                await _finalize(s, row, run, TERMINAL[row.remote_status])
                return
            if row.cancel_requested and row.remote_status not in TERMINAL:
                row.next_poll_at = now + timedelta(seconds=15)
                return
        row.detail = {**(row.detail or {}), "errors": 0}
        row.next_poll_at = now + timedelta(seconds=cfg.poll_seconds)
    except CursorError as e:
        errs = int((row.detail or {}).get("errors", 0)) + 1
        row.detail = {**(row.detail or {}), "errors": errs, "last_error": str(e)[:300]}
        if e.status == 404 and row.status == "running":
            await _finalize(s, row, None, "error", "اجرای Cursor یافت نشد (حذف یا آرشیو شده است).")
            return
        row.next_poll_at = now + timedelta(seconds=min(cfg.poll_seconds * (2 ** min(errs, 5)), 3600))


async def poll_due(limit: int = 10) -> int:
    """پایش اجراهای فعال سررسیده؛ ردیف‌ها با SKIP LOCKED قفل می‌شوند تا دو Worker هم‌زمان پایش نکنند."""
    done = 0
    async with session_scope() as s:
        cfg, key = await config(s)
        if not key:
            return 0
        now = utcnow()
        rows = (await s.execute(select(ExternalAgentRun).where(
            ExternalAgentRun.status.in_(ACTIVE),
            (ExternalAgentRun.next_poll_at.is_(None)) | (ExternalAgentRun.next_poll_at <= now))
            .order_by(ExternalAgentRun.next_poll_at.asc().nullsfirst()).limit(limit)
            .with_for_update(skip_locked=True))).scalars().all()
        if not rows:
            return 0
        c = CursorClient(key, cfg.base_url)
        try:
            for row in rows:
                await _refresh(s, c, row, cfg)
                done += 1
        finally:
            await c.aclose()
    return done


def serialize(r: ExternalAgentRun) -> dict[str, Any]:
    return {"id": str(r.id), "executor": r.executor, "agent_id": r.agent_id, "remote_run_id": r.remote_run_id,
            "status": r.status, "status_label": STATUS_FA.get(r.status, r.status), "remote_status": r.remote_status,
            "repo_url": r.repo_url, "starting_ref": r.starting_ref, "branch": r.branch, "pr_url": r.pr_url,
            "agent_url": r.agent_url, "title": r.title, "model": r.model, "auto_create_pr": r.auto_create_pr,
            "result_summary": r.result_summary, "error": r.error, "requested_by": r.requested_by,
            "cancel_requested": r.cancel_requested, "run_id": str(r.run_id) if r.run_id else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "cost_status": r.cost_status, "poll_count": r.poll_count,
            "duration_ms": (r.detail or {}).get("duration_ms")}
