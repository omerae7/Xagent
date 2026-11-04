#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
#  آزمون انتهابه‌انتهای «xagent update» روی یک نصب آزمایشی کامل (Docker)
#
#   ۰. نصب نسخه فعلی روی «شاخه قدیمی» + داده نمونه + نصب ابزار ثابت با bootstrap.sh (مسیر مهاجرت نسخه قدیم)
#   ۱. Migration خراب          ← باید پیش از استقرار متوقف شود؛ دیتابیس و سرویس دست‌نخورده
#   ۲. سلامت خراب پس از Migration ← Rollback خودکار تگ ایمیج و بازیابی دیتابیس
#   ۳. به‌روزرسانی موفق با Migration و تغییرات محلی ← حفظ داده، patch/stash، بازگرداندن فایل اپراتور
#   ۴. xagent rollback دستی     ← بازگشت نسخه و Schema، نگه‌داری داده‌های پس از به‌روزرسانی
#   ۵. نبود اسرار در گزارش به‌روزرسانی
#
#  استفاده: tests/updater/e2e.sh [WORKDIR]     (نیازمند Docker و پورت‌های آزاد 80/443)
# ════════════════════════════════════════════════════════════════════
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
W="${1:-$(mktemp -d)}"; mkdir -p "$W"; W="$(cd "$W" && pwd)"
T="$W/home"; REMOTE="$W/remote.git"; WORK="$W/work"
export XAGENT_CONF="$W/xagent.conf" XAGENT_LIB="$W/lib" XAGENT_BIN="$W/bin"
export XAGENT_HEALTH_TRIES="${XAGENT_HEALTH_TRIES:-40}" XAGENT_MIN_FREE_GB="${XAGENT_MIN_FREE_GB:-2}"
mkdir -p "$XAGENT_LIB" "$XAGENT_BIN"

pass() { echo -e "\033[1;32m✅ $*\033[0m"; }
fail() { echo -e "\033[1;31m❌ $*\033[0m"; tail -40 "$T/data/control/update.log" 2>/dev/null || true; exit 1; }
dc() { docker compose --project-directory "$T/app" -f "$T/app/compose.yaml" --env-file "$T/app/.env" "$@"; }
q() { dc exec -T postgres psql -U xagent -d "${2:-xagent}" -tAc "$1" | tr -d '\r'; }
xa() { local rc=0; "$XAGENT_BIN/xagent" "$@" >"$W/xa.raw" 2>&1 </dev/null || rc=$?; sed 's/\x1b\[[0-9;]*m//g' "$W/xa.raw" | grep -v '^ Container' || true; return $rc; }
state() { python3 -c "import json;print(json.load(open('$T/data/control/update.status'))['state'])"; }
head_commit() { git -C "$T/app" rev-parse HEAD; }

# ── آماده‌سازی مخزن راه دور آزمایشی ──
BASE="$(git -C "$REPO" rev-parse HEAD)"
rm -rf "$REMOTE" "$WORK" "$T"
git clone -q --bare "$REPO" "$REMOTE"
git -C "$REMOTE" branch -f main "$BASE"; git -C "$REMOTE" symbolic-ref HEAD refs/heads/main
# سرور قدیمی روی شاخه دیگری نصب شده است (مثل fix/... روی VPS فعلی)
git -C "$REMOTE" branch -f legacy-branch "$BASE"
git clone -q "$REMOTE" "$WORK"
git -C "$WORK" config user.email e2e@xagent.local; git -C "$WORK" config user.name "X Agent E2E"
HEAD_REV="$(python3 - "$REPO/services/api/migrations/versions" <<'PY'
import pathlib, re, sys
revs, downs = {}, set()
for f in pathlib.Path(sys.argv[1]).glob("*.py"):
    t = f.read_text(encoding="utf-8")
    r = re.search(r"^revision[^=]*=\s*['\"]([^'\"]+)", t, re.M)
    d = re.search(r"^down_revision[^=]*=\s*['\"]([^'\"]+)", t, re.M)
    if r: revs[r.group(1)] = f
    if d: downs.add(d.group(1))
print(next(r for r in revs if r not in downs))
PY
)"
echo "Schema فعلی: $HEAD_REV"
mkmig() {  # mkmig <rev> <sql...>
  local rev="$1"; shift
  { printf '"""e2e %s"""\nfrom alembic import op\n\nrevision = "%s"\ndown_revision = "%s"\nbranch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n' "$rev" "$rev" "$HEAD_REV"
    for sql in "$@"; do printf '    op.execute("%s")\n' "$sql"; done
    printf '\n\ndef downgrade() -> None:\n    pass\n'; } > "$WORK/services/api/migrations/versions/${rev}_e2e.py"
}
publish() { git -C "$WORK" push -q -f "$REMOTE" "HEAD:main"; }

# ── ۰. نصب نسخه پایه ──
git clone -q --branch legacy-branch "$REMOTE" "$T/app"
mkdir -p "$T/data/control" "$T/runner-home" "$T/backups"
chown -R 1001:1001 "$T/data"; chown 1000:1000 "$T/runner-home"
umask 077
cat > "$T/app/.env" <<EOF
XAGENT_ENV=production
XAGENT_HOME=$T
XAGENT_SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DESKTOP_TOKEN=$(openssl rand -hex 16)
RUNNER_TOKEN=$(openssl rand -hex 16)
XAGENT_PUBLIC_URL=https://localhost
XAGENT_SITE_ADDRESS=localhost
XAGENT_TLS=internal
XAGENT_REPO_URL=$REMOTE
XAGENT_REPO_BRANCH=legacy-branch
XAGENT_IMAGE_TAG=${BASE:0:12}
COMPOSE_PROFILES=
XAGENT_SANDBOX=runner
EOF
umask 022
dc build >/dev/null
dc up -d >/dev/null 2>&1
for i in $(seq 1 60); do dc exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" >/dev/null 2>&1 && break; [ "$i" = 60 ] && fail "نصب پایه بالا نیامد"; sleep 3; done
q "CREATE TABLE e2e_probe(id int primary key, note text); INSERT INTO e2e_probe VALUES (1,'before')" >/dev/null
bash "$REPO/infra/updater/bootstrap.sh" --dir "$T" --ref main --no-update >/dev/null
[ -x "$XAGENT_LIB/xagent-update" ] && [ -L "$XAGENT_BIN/xagent" ] || fail "bootstrap ابزار ثابت را نصب نکرد"
pass "۰. نصب پایه (${BASE:0:12}) و نصب ابزار ثابت با bootstrap"

# ── ۱. Migration خراب ──
git -C "$WORK" checkout -q -B e2e-broken-mig "$BASE"
mkmig e2e1 "CREATE TABLE e2e_tmp(id int)" "SELECT * FROM e2e_table_that_does_not_exist"
git -C "$WORK" add -A; git -C "$WORK" commit -qm "e2e: broken migration"; publish
if xa update --ref main --yes >"$W/s1.log"; then fail "۱. به‌روزرسانی با Migration خراب نباید موفق شود"; fi
[ "$(state)" = "failed" ] || fail "۱. وضعیت باید failed باشد"
[ "$(head_commit)" = "$BASE" ] || fail "۱. نسخه نباید تغییر کند"
[ "$(q 'SELECT version_num FROM alembic_version')" = "$HEAD_REV" ] || fail "۱. دیتابیس اصلی تغییر کرده است"
[ -z "$(q "SELECT to_regclass('e2e_tmp')")" ] || fail "۱. جدول Migration خراب در دیتابیس اصلی ساخته شده"
[ -z "$(q "SELECT datname FROM pg_database WHERE datname LIKE 'xagent_migcheck%'" postgres)" ] || fail "۱. دیتابیس آزمون پاک نشد"
pass "۱. Migration خراب روی کپی شناسایی شد؛ دیتابیس و سرویس دست‌نخورده ماندند"

# ── ۲. سلامت خراب پس از Migration ──
git -C "$WORK" checkout -q -B e2e-broken-health "$BASE"
mkmig e2e1 "CREATE TABLE e2e_tmp(id int)" "UPDATE e2e_probe SET note = 'migrated'"
grep -q 'status_code=200 if ok_db and ok_redis else 503' "$WORK/services/api/xagent_api/main.py" || fail "۲. الگوی health برای خراب‌کردن یافت نشد"
sed -i 's/status_code=200 if ok_db and ok_redis else 503/status_code=503/' "$WORK/services/api/xagent_api/main.py"
git -C "$WORK" add -A; git -C "$WORK" commit -qm "e2e: broken health"; publish
XAGENT_HEALTH_TRIES=12 xa update --ref main --yes >"$W/s2.log" && fail "۲. نسخه ناسالم نباید تثبیت شود"
[ "$(state)" = "rolled_back" ] || fail "۲. وضعیت باید rolled_back باشد (فعلی: $(state))"
[ "$(head_commit)" = "$BASE" ] || fail "۲. کد باید به نسخه پایه برگردد"
[ "$(q 'SELECT version_num FROM alembic_version')" = "$HEAD_REV" ] || fail "۲. Schema باید بازگردد"
[ "$(q 'SELECT note FROM e2e_probe WHERE id=1')" = "before" ] || fail "۲. داده باید به پیش از Migration برگردد"
[ -n "$(q "SELECT datname FROM pg_database WHERE datname LIKE 'xagent_failed_%'" postgres)" ] || fail "۲. دیتابیس ناموفق باید برای بررسی نگه داشته شود"
dc ps --format '{{.Service}} {{.Image}}' | grep -q "api xagent/backend:${BASE:0:12}" || fail "۲. API باید روی ایمیج قبلی باشد"
pass "۲. سلامت خراب ← Rollback خودکار ایمیج و دیتابیس؛ دیتابیس ناموفق نگه داشته شد"

# ── ۳. به‌روزرسانی موفق با Migration و تغییرات محلی ──
git -C "$WORK" checkout -q -B e2e-good "$BASE"
mkmig e2e1 "CREATE TABLE e2e_tmp(id int)" "UPDATE e2e_probe SET note = 'migrated'"
git -C "$WORK" add -A; git -C "$WORK" commit -qm "e2e: good migration"; publish
GOOD="$(git -C "$WORK" rev-parse HEAD)"
echo "# local edit" >> "$T/app/README.md"; echo "operator" > "$T/app/operator-notes.txt"
xa update --ref main --yes >"$W/s3.log" || fail "۳. به‌روزرسانی موفق انجام نشد"
[ "$(state)" = "done" ] || fail "۳. وضعیت باید done باشد"
[ "$(head_commit)" = "$GOOD" ] || fail "۳. نسخه جدید فعال نشد"
[ "$(q 'SELECT version_num FROM alembic_version')" = "e2e1" ] || fail "۳. Migration اعمال نشد"
[ "$(q 'SELECT note FROM e2e_probe WHERE id=1')" = "migrated" ] || fail "۳. داده حفظ/مهاجرت نشد"
ls "$T"/backups/local-changes-*.patch >/dev/null 2>&1 || fail "۳. patch تغییرات محلی ساخته نشد"
git -C "$T/app" stash list | grep -q xagent-update- || fail "۳. stash تغییرات محلی ساخته نشد"
[ -f "$T/app/operator-notes.txt" ] || fail "۳. فایل اپراتور بازگردانده نشد"
grep -q '"previous_commit":"'"$BASE"'"' "$T/data/control/version.json" || fail "۳. version.json نسخه قبلی را ثبت نکرد"
dc exec -T redis redis-cli GET xagent:worker:heartbeat | grep -q . || fail "۳. Worker فعال نیست"
grep -q '^XAGENT_REPO_BRANCH=main$' "$T/app/.env" || fail "۳. شاخه منبع پس از مهاجرت به main تغییر نکرد"
pass "۳. به‌روزرسانی موفق؛ داده و تنظیمات حفظ شد؛ تغییرات محلی در patch/stash و فایل اپراتور بازگردانده شد"

# ── ۴. Rollback دستی ──
q "INSERT INTO e2e_probe VALUES (2,'after-update')" >/dev/null
xa rollback --yes >"$W/s4.log" || fail "۴. xagent rollback ناموفق بود"
[ "$(head_commit)" = "$BASE" ] || fail "۴. کد به نسخه قبل برنگشت"
[ "$(q 'SELECT version_num FROM alembic_version')" = "$HEAD_REV" ] || fail "۴. Schema برنگشت"
AFTER_DB="$(q "SELECT datname FROM pg_database WHERE datname LIKE 'xagent_after_update_%'" postgres | head -1)"
[ -n "$AFTER_DB" ] && [ "$(q 'SELECT note FROM e2e_probe WHERE id=2' "$AFTER_DB")" = "after-update" ] || fail "۴. داده‌های پس از به‌روزرسانی نگه داشته نشدند"
pass "۴. xagent rollback: نسخه و Schema بازگشت؛ داده‌های پس از به‌روزرسانی در $AFTER_DB حفظ شد"
xa update --dry-run >"$W/s4b.log" || fail "۴. پس از Rollback، xagent update بدون --ref باید نسخه main را پیشنهاد دهد"
grep -q "${GOOD:0:12}" "$W/s4b.log" || fail "۴. نسخه هدف پیشنهادی باید آخرین main باشد"
pass "۴ب. «xagent update» بدون گزینه پس از مهاجرت، شاخه main را دنبال می‌کند"

# ── ۵. اسرار ──
for k in POSTGRES_PASSWORD XAGENT_SECRET_KEY RUNNER_TOKEN DESKTOP_TOKEN; do
  v="$(grep "^$k=" "$T/app/.env" | cut -d= -f2)"
  if grep -qF "$v" "$T/data/control/update.log" "$W"/s*.log; then fail "۵. مقدار $k در گزارش‌ها نشت کرده است"; fi
done
pass "۵. هیچ سری در گزارش‌های به‌روزرسانی دیده نشد"

dc down -v >/dev/null 2>&1 || true
echo "همه سناریوهای به‌روزرسانی موفق بودند."
