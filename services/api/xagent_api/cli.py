"""ابزار خط فرمان مدیریتی: بازنشانی رمز کارفرما و نمایش وضعیت.

نمونه: docker compose exec api xagent-entrypoint cli reset-password
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy import select

from xagent_core.db import dispose_engine, session_scope
from xagent_core.models import User
from xagent_core.security import hash_password


async def reset_password(username: str, password: str | None) -> None:
    pw = password or secrets.token_urlsafe(12)
    async with session_scope() as s:
        user = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, role="owner", password_hash=hash_password(pw))
            s.add(user)
        else:
            user.password_hash = hash_password(pw)
            user.token_version += 1
            user.failed_logins = 0
            user.locked_until = None
    await dispose_engine()
    print(f"✅ رمز «{username}» تنظیم شد: {pw}")


def main() -> None:
    p = argparse.ArgumentParser(prog="xagent-cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-password", help="بازنشانی رمز کارفرما")
    r.add_argument("--username", default="admin")
    r.add_argument("--password")
    args = p.parse_args()
    if args.cmd == "reset-password":
        asyncio.run(reset_password(args.username, args.password))


if __name__ == "__main__":
    main()
