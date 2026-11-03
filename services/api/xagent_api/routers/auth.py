"""احراز هویت: ورود با محدودیت نرخ و قفل موقت، خروج، تغییر رمز و خروج از همه دستگاه‌ها."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.config import get_settings
from xagent_core.db import utcnow
from xagent_core.models import User
from xagent_core.security import create_access_token, hash_password, verify_password

from ..deps import COOKIE, client_ip, current_user, get_session, ser

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


def _set_cookie(resp: Response, token: str) -> None:
    s = get_settings()
    resp.set_cookie(
        COOKIE, token, httponly=True, secure=s.cookie_secure, samesite="strict",
        max_age=s.access_token_minutes * 60, path="/",
    )


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    ip = client_ip(request)
    r = bus.get_redis()
    key = f"xagent:login:{ip}"
    attempts = int(await r.get(key) or 0)
    if attempts >= 10:
        raise HTTPException(429, "تلاش‌های ناموفق زیاد؛ ۱۵ دقیقه دیگر دوباره امتحان کنید.")
    user = (await session.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    now = utcnow()
    if user and user.locked_until and user.locked_until > now:
        raise HTTPException(423, "حساب به‌طور موقت قفل است؛ کمی بعد تلاش کنید.")
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        await r.incr(key)
        await r.expire(key, 900)
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= 5:
                user.locked_until = now + timedelta(minutes=15)
                user.failed_logins = 0
        await audit(session, body.username, "auth.login_failed", ip=ip)
        await session.commit()
        raise HTTPException(401, "نام کاربری یا رمز عبور نادرست است")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    await r.delete(key)
    token = create_access_token(str(user.id), user.token_version)
    _set_cookie(response, token)
    await audit(session, user.username, "auth.login", ip=ip)
    return {"user": ser(user), "token": token}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"user": ser(user)}


@router.post("/password")
async def change_password(body: PasswordIn, request: Request, response: Response,
                          user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(400, "رمز فعلی نادرست است")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    await audit(session, user.username, "auth.password_changed", ip=client_ip(request))
    _set_cookie(response, create_access_token(str(user.id), user.token_version))
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(response: Response, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    user.token_version += 1
    await audit(session, user.username, "auth.logout_all")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
