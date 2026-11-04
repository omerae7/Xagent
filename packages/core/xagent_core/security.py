"""امنیت: هش رمز عبور (Argon2id)، توکن JWT و رمزنگاری کلیدهای API (Fernet)."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings
from .redact import register_secret

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, Exception):
        return False


def _derive(purpose: str) -> bytes:
    secret = get_settings().secret_key.encode()
    return hashlib.sha256(purpose.encode() + b"|" + secret).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive("xagent-fernet-v1")))


def encrypt_secret(plain: str | None) -> bytes | None:
    if not plain:
        return None
    return _fernet().encrypt(plain.encode())


def decrypt_secret(token: bytes | None) -> str | None:
    if not token:
        return None
    try:
        plain = _fernet().decrypt(bytes(token)).decode()
    except InvalidToken:
        return None
    register_secret(plain)  # هر کلید رمزگشایی‌شده در متن خطا/لاگ پنهان می‌شود
    return plain


def mask_secret(plain: str | None) -> str:
    if not plain:
        return ""
    if len(plain) <= 8:
        return "••••"
    return f"{plain[:4]}••••{plain[-4:]}"


def create_access_token(user_id: str, token_version: int, extra: dict[str, Any] | None = None) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.access_token_minutes)).timestamp()),
        "jti": secrets.token_hex(8),
        **(extra or {}),
    }
    return jwt.encode(payload, _derive("xagent-jwt-v1"), algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, _derive("xagent-jwt-v1"), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
