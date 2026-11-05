"""ابزار خط فرمان مدیریتی: بازنشانی رمز کارفرما و نمایش وضعیت.

نمونه: docker compose exec api xagent-entrypoint cli reset-password
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy import select, text

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


# ستون‌های JSON/متنی که ممکن است در سوابق تاریخی اسرار متن‌ساده داشته باشند (فقط خواندن)
_INVENTORY = {
    "memory_revisions": ("MemoryRevision", ("before", "after", "baseline", "reason")),
    "config_changes": ("ConfigChange", ("before", "after", "summary", "reason")),
    "tool_versions": ("ToolVersion", ("manifest", "test_output")),
    "tools_installed": ("InstalledTool", ("config", "source_url", "last_error")),
    "audit_logs": ("AuditLog", ("detail",)),
    "notifications": ("Notification", ("title", "body")),
}


async def secrets_inventory(dispose: bool = True, batch: int = 500) -> dict:
    """فهرست‌برداری فقط‌خواندنی اسرار متن‌ساده در سوابق: جدول، شناسه، مسیر و نوع — هرگز مقدار.

    هیچ رکوردی حذف، بازنویسی یا مهروموم نمی‌شود و هیچ کلیدی خودکار چرخانده نمی‌شود.
    """
    from xagent_core import models
    from xagent_core.redact import secret_paths

    findings: list[dict] = []
    async with session_scope() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        for table, (model_name, cols) in _INVENTORY.items():
            model = getattr(models, model_name)
            offset = 0
            while True:
                rows = (await s.execute(select(model).order_by(model.id).limit(batch).offset(offset))).scalars().all()
                if not rows:
                    break
                for row in rows:
                    for col in cols:
                        for path, kind in secret_paths(getattr(row, col, None), col):
                            findings.append({"table": table, "id": str(row.id), "path": path, "kind": kind})
                offset += batch
        await s.rollback()
    if dispose:
        await dispose_engine()
    summary: dict[str, int] = {}
    for f in findings:
        summary[f["table"]] = summary.get(f["table"], 0) + 1
    print(f"فهرست اسرار متن‌ساده (فقط مسیر، بدون مقدار): {len(findings)} مورد")
    for t, n in sorted(summary.items()):
        print(f"  {t}: {n}")
    for f in findings[:200]:
        print(f"  - {f['table']} {f['id']} {f['path']} ({f['kind']})")
    return {"findings": findings, "summary": summary}


def main() -> None:
    p = argparse.ArgumentParser(prog="xagent-cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-password", help="بازنشانی رمز کارفرما")
    r.add_argument("--username", default="admin")
    r.add_argument("--password")
    sub.add_parser("secrets-inventory", help="فهرست فقط‌خواندنی محل اسرار متن‌ساده در سوابق (بدون چاپ مقدار)")
    args = p.parse_args()
    if args.cmd == "reset-password":
        asyncio.run(reset_password(args.username, args.password))
    elif args.cmd == "secrets-inventory":
        asyncio.run(secrets_inventory())


if __name__ == "__main__":
    main()
