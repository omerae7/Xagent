"""وابستگی‌های مشترک API: نشست دیتابیس، احراز هویت، محافظت CSRF و سریال‌سازی."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException, Request, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect as sa_inspect

from xagent_core.db import get_sessionmaker
from xagent_core.models import User
from xagent_core.security import decode_access_token

COOKIE = "xa_token"
HIDDEN = {"embedding", "tsv", "api_key_enc", "password_hash", "token_version", "failed_logins", "state"}


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def _token_from(request: Request | WebSocket) -> str | None:
    tok = request.cookies.get(COOKIE)
    if tok:
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


async def resolve_user(session: AsyncSession, token: str | None) -> User | None:
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    try:
        user = await session.get(User, uuid.UUID(payload["sub"]))
    except (ValueError, KeyError):
        return None
    if user is None or not user.is_active or user.token_version != payload.get("tv"):
        return None
    return user


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    user = await resolve_user(session, _token_from(request))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ابتدا وارد شوید")
    # محافظت CSRF: درخواست‌های تغییردهنده باید هدر سفارشی داشته باشند (کوکی SameSite=Strict هم هست)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.cookies.get(COOKIE):
        if request.headers.get("x-xagent") != "1":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "درخواست نامعتبر (CSRF)")
    return user


async def owner_user(user: User = Depends(current_user)) -> User:
    if user.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "فقط کارفرما مجاز است")
    return user


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def jsonable(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return None
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    return v


def ser(obj: Any, exclude: set[str] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if obj is None:
        return {}
    skip = HIDDEN | (exclude or set())
    out = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        if attr.key in skip:
            continue
        out[attr.key] = jsonable(getattr(obj, attr.key))
    if extra:
        out.update(jsonable(extra))
    return out


def parse_uuid(v: str) -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "شناسه نامعتبر") from None
