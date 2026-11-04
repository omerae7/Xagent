"""اسکیمای کامل دیتابیس X Agent.

لایه‌ها:
- احراز هویت و تنظیمات (users, settings_kv, ai_providers, ai_models, harness_rules)
- گفتگو و اجرا (conversations, messages, runs, run_steps, approvals, uploads)
- مصرف و ارزیابی (usage_events, system_metrics)
- وظایف، روتین‌ها و CRM (tasks, routines, contacts, projects, crm_interactions)
- حافظه ۵ لایه (messages/working, behavioral_rules, semantic_facts, episodes, experiences)
- Snapshot/Undo یادگیری‌ها (memory_revisions) و مهارت‌ها (skills)
- ابزارها (tools_installed, tool_versions)
- رویا (dream_reports)، رویدادها (events_inbox)، اعلان‌ها و لاگ حسابرسی
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, IdMixin, TimestampMixin, utcnow

EMBED_DIM = 768
FTS_EXPR = "to_tsvector('simple', xa_normalize(coalesce({col}, '')))"


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), **kw)


# ───────────────────────────── احراز هویت و تنظیمات ─────────────────────────────


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128), default="کارفرما")
    role: Mapped[str] = mapped_column(String(16), default="owner")  # owner | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SettingKV(Base):
    __tablename__ = "settings_kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = _ts(onupdate=utcnow)


class AIProvider(IdMixin, TimestampMixin, Base):
    __tablename__ = "ai_providers"
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))  # openai | anthropic | google | openrouter | ...
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_error: Mapped[str | None] = mapped_column(Text)


class AIModel(IdMixin, TimestampMixin, Base):
    __tablename__ = "ai_models"
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_providers.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # {"vision": bool, "tools": bool, "stt": bool, "tts": bool, "audio_in": bool, "json": bool, "embedding": bool}
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    context_window: Mapped[int] = mapped_column(Integer, default=128000)
    price_in: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)  # دلار به ازای هر میلیون توکن
    price_out: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    price_cache_read: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    price_cache_write: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    # منبع هر قابلیت: {"tools": {"value": true, "source": "probe|provider|catalog|manual|heuristic", "at": "..."}}
    capability_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")  # manual|catalog|discovered
    pricing_source: Mapped[str] = mapped_column(String(24), default="unknown", server_default="unknown")
    pricing_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # سلامت عملیاتی: unknown | ok | degraded | failing
    health: Mapped[str] = mapped_column(String(12), default="unknown", server_default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_class: Mapped[str | None] = mapped_column(String(32))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("provider_id", "model_name"),)


class HarnessRule(IdMixin, Base):
    __tablename__ = "harness_rules"
    category: Mapped[str] = mapped_column(String(48), unique=True)
    mode: Mapped[str] = mapped_column(String(16), default="ask")  # auto | ask | conditional | forbidden
    rule_text: Mapped[str] = mapped_column(Text, default="")
    # شروط قطعی ساختاریافته که از rule_text استخراج/تعریف می‌شوند (allow/deny الگوها)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = _ts(onupdate=utcnow)


# ───────────────────────────── گفتگو و اجرا ─────────────────────────────


class Conversation(IdMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    title: Mapped[str] = mapped_column(String(256), default="گفتگوی جدید")
    channel: Mapped[str] = mapped_column(String(16), default="web")  # web | telegram | voice | routine | system
    external_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    model_selection: Mapped[str] = mapped_column(String(64), default="auto")
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    # خلاصه فشرده حافظه کاری (Working RAM) برای کنترل بودجه کانتکست
    summary: Mapped[str | None] = mapped_column(Text)
    summary_upto: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, default=0)


class Message(IdMixin, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system | tool
    content: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _ts()
    tsv = mapped_column(TSVECTOR, Computed(FTS_EXPR.format(col="content"), persisted=True))
    __table_args__ = (
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
    )


class Upload(IdMixin, Base):
    __tablename__ = "uploads"
    filename: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _ts()


class Run(IdMixin, Base):
    __tablename__ = "runs"
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    routine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routines.id", ondelete="SET NULL"), index=True
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="chat")  # chat|routine|telegram|voice|api|event
    # queued | running | waiting_approval | paused | succeeded | failed | stopped | cancelled
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    input: Mapped[str] = mapped_column(Text, default="")
    model_override: Mapped[str | None] = mapped_column(String(64))
    route: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    goal: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Checkpoint کامل برای ادامه امن پس از ری‌استارت یا تایید
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    steps_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cache_read: Mapped[int] = mapped_column(Integer, default=0)
    cache_write: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts(index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunStep(IdMixin, Base):
    __tablename__ = "run_steps"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    # route|goal|plan|memory|llm|tool|validate|recover|learn|approval|stoploss|info|answer
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error|skipped|blocked
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    model_label: Mapped[str | None] = mapped_column(String(128))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(IdMixin, Base):
    __tablename__ = "approvals"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(48))
    tool_name: Mapped[str] = mapped_column(String(128))
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


# ───────────────────────────── مصرف و پایش ─────────────────────────────


class UsageEvent(IdMixin, Base):
    __tablename__ = "usage_events"
    ts: Mapped[datetime] = _ts(index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    provider_kind: Mapped[str] = mapped_column(String(32), default="")
    model_name: Mapped[str] = mapped_column(String(128), default="")
    purpose: Mapped[str] = mapped_column(String(32), default="")  # manager|worker|goal|planning|...
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cache_read: Mapped[int] = mapped_column(Integer, default=0)
    cache_write: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    # هزینه با قیمت محافظه‌کارانه (مدل بدون قیمت معتبر) برآورد شده است
    cost_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class SystemMetric(Base):
    __tablename__ = "system_metrics"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    cpu: Mapped[float] = mapped_column(Float)
    mem: Mapped[float] = mapped_column(Float)
    swap: Mapped[float] = mapped_column(Float, default=0)
    disk: Mapped[float] = mapped_column(Float)
    load1: Mapped[float] = mapped_column(Float, default=0)
    net_rx: Mapped[int] = mapped_column(BigInteger, default=0)
    net_tx: Mapped[int] = mapped_column(BigInteger, default=0)
    active_runs: Mapped[int] = mapped_column(Integer, default=0)


# ───────────────────────────── وظایف، روتین‌ها و CRM ─────────────────────────────


class Contact(IdMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    kind: Mapped[str] = mapped_column(String(16), default="colleague")  # colleague | contact | client | vendor
    name: Mapped[str] = mapped_column(String(128))
    role_title: Mapped[str | None] = mapped_column(String(128))
    company: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(256))
    telegram_username: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(48)), default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | on_hold | done | archived
    color: Mapped[str] = mapped_column(String(16), default="#5b8cff")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    member_ids: Mapped[list[str]] = mapped_column(ARRAY(String(36)), default=list)


class CRMInteraction(IdMixin, Base):
    __tablename__ = "crm_interactions"
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16), default="note")  # telegram|call|meeting|email|note|web
    direction: Mapped[str] = mapped_column(String(8), default="in")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()


class Task(IdMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="todo", index=True)  # todo|in_progress|waiting|done|cancelled
    priority: Mapped[str] = mapped_column(String(8), default="normal")  # low|normal|high|urgent
    assignee_type: Mapped[str] = mapped_column(String(16), default="agent")  # agent | contact | owner
    assignee_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(16), default="owner")
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(48)), default=list)
    sort_order: Mapped[float] = mapped_column(Float, default=0)


class Routine(IdMixin, TimestampMixin, Base):
    __tablename__ = "routines"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(String(16), default="cron")  # cron | interval | event | once
    cron_expr: Mapped[str | None] = mapped_column(String(64))
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # فیلتر قطعی رویداد: {"source": "telegram", "chat_ids": [...], "keywords": [...], "regex": "..."}
    event_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tehran")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    time_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    max_steps: Mapped[int | None] = mapped_column(Integer)
    max_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_status: Mapped[str | None] = mapped_column(String(16))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    circuit_state: Mapped[str] = mapped_column(String(12), default="closed")  # closed | open | half_open
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(16), default="owner")
    run_count: Mapped[int] = mapped_column(Integer, default=0)


# ───────────────────────────── حافظه ۵ لایه ─────────────────────────────


class MemoryCandidate(IdMixin, Base):
    """نکات مهم شناسایی‌شده در حافظه چت که منتظر بررسی تعارض و انتقال هستند."""

    __tablename__ = "memory_candidates"
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    target_layer: Mapped[str] = mapped_column(String(16))  # behavioral | semantic
    section: Mapped[str | None] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)  # آیا مستقیماً از کارفرما آمده است
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|promoted|rejected|conflict
    conflict_with: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class BehavioralRule(IdMixin, TimestampMixin, Base):
    __tablename__ = "behavioral_rules"
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=50)  # بزرگ‌تر = مهم‌تر
    scope: Mapped[str] = mapped_column(String(64), default="global")  # global|chat|telegram|routine|tool:<name>
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|probationary|disabled|purged
    source: Mapped[str] = mapped_column(String(16), default="owner")  # owner | agent | dream
    source_ref: Mapped[str | None] = mapped_column(String(128))


class SemanticFact(IdMixin, TimestampMixin, Base):
    __tablename__ = "semantic_facts"
    section: Mapped[str] = mapped_column(String(16))  # agent | owner
    # فضای نام حافظه: global (مدیر و کارمندان مشترک) یا employee:<id> (حافظه خصوصی کارمند)
    scope: Mapped[str] = mapped_column(String(64), default="global", server_default="global", index=True)
    key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="owner")
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(96))
    tsv = mapped_column(TSVECTOR, Computed(FTS_EXPR.format(col="content"), persisted=True))
    __table_args__ = (
        UniqueConstraint("scope", "section", "key", name="uq_semantic_facts_scope_section_key"),
        Index("ix_semantic_tsv", "tsv", postgresql_using="gin"),
    )


class Episode(IdMixin, Base):
    """حافظه رویدادی و لاگ حسابرسی عملیات — هرگز حذف نمی‌شود (Trigger دیتابیسی)."""

    __tablename__ = "episodes"
    ts: Mapped[datetime] = _ts(index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # task|routine|message|tool|system|chat
    title: Mapped[str] = mapped_column(String(256))
    summary: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    routine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    tools: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), index=True)  # success|failed|running|stopped
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scope: Mapped[str] = mapped_column(String(64), default="global", server_default="global", index=True)


class Experience(IdMixin, TimestampMixin, Base):
    __tablename__ = "experiences"
    signature: Mapped[str] = mapped_column(Text)  # شرح نرمال‌شده کار
    scope: Mapped[str] = mapped_column(String(64), default="global", server_default="global", index=True)
    title: Mapped[str] = mapped_column(String(256))
    approach: Mapped[str] = mapped_column(Text)  # روش/گام‌ها
    tool_sequence: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)
    outcome: Mapped[str] = mapped_column(String(24), default="verified_success")  # verified_success|failed_attempt|obsolete
    status: Mapped[str] = mapped_column(String(16), default="probationary")  # active|probationary|purged
    routine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(96))
    tsv = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', xa_normalize(coalesce(signature,'') || ' ' || coalesce(title,'')))",
            persisted=True,
        ),
    )
    __table_args__ = (Index("ix_experiences_tsv", "tsv", postgresql_using="gin"),)


class MemoryRevision(IdMixin, Base):
    """Snapshot قبل از هر یادگیری — پایه Undo و Auto-Rollback."""

    __tablename__ = "memory_revisions"
    entity_type: Mapped[str] = mapped_column(String(24), index=True)  # behavioral_rule|semantic_fact|experience|skill|tool
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(16))  # create | update | delete | status
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(16), default="agent")  # owner|agent|dream|evaluator
    origin: Mapped[str] = mapped_column(String(16), default="chat")  # chat|dream|routine|panel|telegram
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # applied | probationary | confirmed | reverted
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    probation_runs: Mapped[int] = mapped_column(Integer, default=0)
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    observed: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[str | None] = mapped_column(String(16))
    revert_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(index=True)


class Skill(IdMixin, TimestampMixin, Base):
    __tablename__ = "skills"
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    trigger_hint: Mapped[str] = mapped_column(Text, default="")
    procedure: Mapped[str] = mapped_column(Text, default="")
    tool_sequence: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)
    status: Mapped[str] = mapped_column(String(16), default="probationary")  # probationary|active|disabled|purged
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="dream")
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_tokens: Mapped[float] = mapped_column(Float, default=0)
    avg_duration_ms: Mapped[float] = mapped_column(Float, default=0)
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    test_status: Mapped[str | None] = mapped_column(String(16))
    test_output: Mapped[str | None] = mapped_column(Text)


# ───────────────────────────── ابزارها ─────────────────────────────


class InstalledTool(IdMixin, TimestampMixin, Base):
    __tablename__ = "tools_installed"
    name: Mapped[str] = mapped_column(String(96), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16))  # mcp_stdio | mcp_http | python_plugin | cli
    source_url: Mapped[str | None] = mapped_column(String(512))
    ref: Mapped[str | None] = mapped_column(String(128))
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="installing")  # installing|ready|error|disabled
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    harness_category: Mapped[str] = mapped_column(String(48), default="external_tool")
    last_error: Mapped[str | None] = mapped_column(Text)
    tool_schemas: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)


class ToolVersion(IdMixin, Base):
    __tablename__ = "tool_versions"
    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tools_installed.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(64))  # commit sha
    ref: Mapped[str | None] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(1024))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    test_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|passed|failed|skipped
    test_output: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


# ───────────────────────────── رویا، رویدادها، اعلان‌ها و حسابرسی ─────────────────────────────


class DreamReport(IdMixin, Base):
    __tablename__ = "dream_reports"
    started_at: Mapped[datetime] = _ts(index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|skipped|failed
    checkpoint_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    events_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    learnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    skills_proposed: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    cleanup: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class EventInbox(IdMixin, Base):
    __tablename__ = "events_inbox"
    source: Mapped[str] = mapped_column(String(24), index=True)  # telegram | webhook | timer | desktop | system
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # new|ignored|routed|processed|failed
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _ts(index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(IdMixin, Base):
    __tablename__ = "notifications"
    ts: Mapped[datetime] = _ts(index=True)
    level: Mapped[str] = mapped_column(String(12), default="info")  # info|success|warning|error|approval
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(256))
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AuditLog(IdMixin, Base):
    """لاگ حسابرسی تغییرناپذیر — حذف و ویرایش آن با Trigger دیتابیس مسدود است."""

    __tablename__ = "audit_logs"
    ts: Mapped[datetime] = _ts(index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))


# ───────────────────────────── مدیر، کارمندان و خوددرمانی ─────────────────────────────


class Employee(IdMixin, TimestampMixin, Base):
    """کارمند هوش مصنوعی: نقش مستقل، مدل و ابزار اختصاصی، گزارش به مدیر."""

    __tablename__ = "employees"
    name: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    model_ids: Mapped[list[str]] = mapped_column(ARRAY(String(36)), default=list)
    tool_groups: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    memory_scope: Mapped[str] = mapped_column(String(16), default="shared")  # shared | private
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="idle")  # idle | working | error
    max_steps: Mapped[int] = mapped_column(Integer, default=12)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0.2"))
    created_by: Mapped[str] = mapped_column(String(32), default="owner")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ConfigChange(IdMixin, Base):
    """هر تغییر تنظیمات (توسط مدیر، کارمند، خوددرمانی یا کارفرما) با قبل/بعد برای Audit و Rollback."""

    __tablename__ = "config_changes"
    created_at: Mapped[datetime] = _ts(index=True)
    actor: Mapped[str] = mapped_column(String(48))  # owner | manager | employee:<name> | self_heal | system
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    target_type: Mapped[str] = mapped_column(String(24), index=True)  # setting|model|provider|tool|employee|routing
    target_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(String(8), default="low")
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # applied | verified | verification_failed | rolled_back | rejected | pending_approval
    status: Mapped[str] = mapped_column(String(24), default="applied", index=True)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[str | None] = mapped_column(String(48))


class Incident(IdMixin, Base):
    """رخداد عملیاتی برای خوددرمانی: تشخیص ← طبقه‌بندی ← اصلاح ← آزمون ← گزارش."""

    __tablename__ = "incidents"
    created_at: Mapped[datetime] = _ts(index=True)
    last_seen_at: Mapped[datetime] = _ts()
    source: Mapped[str] = mapped_column(String(16))  # model | tool | provider | run | system
    target: Mapped[str] = mapped_column(String(128))
    error_class: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    count: Mapped[int] = mapped_column(Integer, default=1)
    # open | fixing | fixed | escalated | ignored
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("source", "target", "error_class"),)


class HostOp(IdMixin, Base):
    """عملیات میزبان (Host Command Broker): درخواست‌کننده، سطح، مجوز، خروجی پاک‌سازی‌شده و نتیجه."""

    __tablename__ = "host_ops"
    created_at: Mapped[datetime] = _ts(index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    op: Mapped[str] = mapped_column(String(64), index=True)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    level: Mapped[int] = mapped_column(Integer, default=1)
    requested_by: Mapped[str] = mapped_column(String(64))  # نام کاربری کارفرما | manager | self_heal | policy
    actor_kind: Mapped[str] = mapped_column(String(16), default="owner")  # owner | manager | policy
    reason: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # queued|pending_approval|running|succeeded|failed|timeout|cancelled|denied|rejected|lost
    host_job: Mapped[str | None] = mapped_column(String(64), index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[str] = mapped_column(Text, default="")
    output_bytes: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
