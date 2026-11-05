"""ممیزی جرم‌شناختی — مرحله ۵، F11: پیش از downgrade مخرب، وضعیت نسخه ناموفق (داده‌های پس از ارتقا) حفظ شود.

تابع واقعی `preserve_failed_release` از `infra/updater/xagent-update.sh` استخراج و روی پایگاه داده یکبارمصرف
PostgreSQL محلی اجرا می‌شود (پوسته `dc_app` به ابزارهای محلی pg هدایت می‌شود). پایگاه داده تولید لمس نمی‌شود.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/updater/xagent-update.sh"
PG = {"PGHOST": "localhost", "PGUSER": "xagent", "PGPASSWORD": "xagent", "PGPORT": "5432"}
pytestmark = pytest.mark.skipif(shutil.which("pg_dump") is None or shutil.which("psql") is None,
                                reason="ابزارهای PostgreSQL محلی در دسترس نیست")


def psql(db: str, sql: str) -> str:
    return subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-tA", "-d", db, "-c", sql], env={**os.environ, **PG},
                          capture_output=True, text=True, check=True).stdout.strip()


def extract_function() -> str:
    text = SCRIPT.read_text()
    m = re.search(r"# >>> preserve_failed_release.*?# <<< preserve_failed_release", text, re.S)
    assert m, "تابع حفظ وضعیت نسخه ناموفق در اسکریپت به‌روزرسان وجود ندارد"
    return m.group(0)


@pytest.fixture
def scratch_db():
    name = f"xa_f11_{uuid.uuid4().hex[:8]}"
    psql("postgres", f"CREATE DATABASE {name}")
    yield name
    psql("postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def run_preserve(db: str, out: Path, before: Path) -> subprocess.CompletedProcess:
    shim = f'''
set -u
DB={db}
dc_app() {{  # dc_app exec -T postgres <cmd …> → اجرای محلی روی پایگاه داده یکبارمصرف
  shift 3; local args=() prev=""
  for a in "$@"; do if [ "$prev" = "-d" ] && [ "$a" = "xagent" ]; then a="$DB"; fi; args+=("$a"); prev="$a"; done
  "${{args[@]}}"
}}
{extract_function()}
preserve_failed_release "{out}" "{before}"
'''
    return subprocess.run(["bash", "-c", shim], env={**os.environ, **PG}, capture_output=True, text=True)


def test_F11_post_upgrade_writes_survive_destructive_downgrade(scratch_db, tmp_path):
    db = scratch_db
    psql(db, "CREATE TABLE e2e_probe(id int primary key, note text); INSERT INTO e2e_probe VALUES (1,'before')")
    before = tmp_path / "pre-upgrade.dump"
    subprocess.run(["pg_dump", "-d", db, "-Fc", "-f", str(before)], env={**os.environ, **PG}, check=True)
    # «ارتقا»: جدول و ستون تازه + داده‌های پذیرفته‌شده پس از ارتقا
    psql(db, "CREATE TABLE new_feature(id int, payload text); INSERT INTO new_feature VALUES (1,'accepted-1'),(2,'accepted-2');"
             "ALTER TABLE e2e_probe ADD COLUMN extra text; UPDATE e2e_probe SET extra='kept-after-upgrade'")
    out = tmp_path / "failed-release.dump"
    res = run_preserve(db, out, before)
    assert res.returncode == 0, res.stderr
    manifest = Path(f"{out}.manifest.txt").read_text()
    assert "new_table new_feature rows=2" in manifest, manifest
    # «downgrade» مخرب نسخه ناموفق
    psql(db, "DROP TABLE new_feature; ALTER TABLE e2e_probe DROP COLUMN extra")
    restore = f"{db}_r"
    psql("postgres", f"CREATE DATABASE {restore}")
    try:
        subprocess.run(["pg_restore", "--exit-on-error", "-d", restore, str(out)], env={**os.environ, **PG}, check=True)
        assert psql(restore, "SELECT string_agg(payload, ',' ORDER BY id) FROM new_feature") == "accepted-1,accepted-2"
        assert psql(restore, "SELECT extra FROM e2e_probe WHERE id=1") == "kept-after-upgrade"
    finally:
        psql("postgres", f"DROP DATABASE IF EXISTS {restore} WITH (FORCE)")
    assert psql(db, "SELECT note FROM e2e_probe WHERE id=1") == "before"  # داده قبلی در دیتابیس اصلی سالم است


def test_F11_failed_copy_blocks_downgrade_path(tmp_path):
    out = tmp_path / "failed-release.dump"
    res = run_preserve("xa_f11_does_not_exist", out, tmp_path / "missing.dump")
    assert res.returncode != 0 and not out.exists(), "کپی ناموفق موفق گزارش شد"
    body = SCRIPT.read_text()
    rb = body[body.index("rollback() {"):]
    assert rb.index("preserve_failed_release") < rb.index("downgrade \"$PREV_REV\""), \
        "downgrade پیش از حفظ وضعیت نسخه ناموفق اجرا می‌شود"
    assert "همه داده‌ها (از جمله داده‌های ثبت‌شده در حین به‌روزرسانی) حفظ شدند" not in body, "ادعای نادرست حفظ همه داده‌ها"
