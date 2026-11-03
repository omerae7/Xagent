#!/usr/bin/env bash
# به‌روزرسان امن هسته X Agent (اجرا روی میزبان توسط systemd؛ بدون سوکت داکر داخل کانتینرها)
# مراحل: پشتیبان دیتابیس ← دریافت کد از GitHub ← ساخت ایمیج‌ها ← اجرا ← بررسی سلامت ← بازگشت در صورت شکست
set -uo pipefail
XAGENT_HOME="${XAGENT_HOME:-/opt/xagent}"
APP="$XAGENT_HOME/app"
CTL="$XAGENT_HOME/data/control"
LOG="$CTL/update.log"
mkdir -p "$CTL" "$XAGENT_HOME/backups"
exec > >(tee -a "$LOG") 2>&1

status() { printf '{"state":"%s","at":"%s","message":"%s"}\n' "$1" "$(date -u +%FT%TZ)" "${2:-}" > "$CTL/update.status"; }
rm -f "$CTL/update.request"
: > "$LOG"
status "running" "شروع به‌روزرسانی"
echo "== به‌روزرسانی X Agent — $(date) =="

cd "$APP" || { status "failed" "پوشه برنامه یافت نشد"; exit 1; }
BRANCH="$(grep -E '^XAGENT_REPO_BRANCH=' .env | cut -d= -f2)"; BRANCH="${BRANCH:-main}"
PREV="$(git rev-parse HEAD)"

echo "→ پشتیبان‌گیری از دیتابیس…"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAMP_ID="$(date +%Y%m%d_%H%M%S)"
BACKUP="$XAGENT_HOME/backups/xagent-$STAMP.dump"
if docker compose exec -T postgres pg_dump -U xagent -d xagent -Fc > "$BACKUP"; then
  echo "  ✓ $BACKUP"
  ls -1t "$XAGENT_HOME"/backups/xagent-*.dump | tail -n +8 | xargs -r rm -f
else
  status "failed" "پشتیبان‌گیری ناموفق بود؛ به‌روزرسانی انجام نشد"; exit 1
fi

echo "→ دریافت کد از GitHub ($BRANCH)…"
if ! git fetch --depth 50 origin "$BRANCH"; then status "failed" "دریافت از GitHub ناموفق بود"; exit 1; fi
git reset --hard "origin/$BRANCH"
NEW="$(git rev-parse HEAD)"
if [ "$NEW" = "$PREV" ]; then
  echo "✓ نسخه فعلی به‌روز است ($NEW)"
  status "done" "نسخه فعلی به‌روز است"; exit 0
fi

DEPLOY_STARTED=0

manual_recovery() {
  status "recovery_required" "$1؛ سرویس‌های API و Worker متوقف مانده‌اند؛ پایگاه‌داده و بکاپ محفوظ‌اند."
  echo "✗ $1" >&2
  echo "بازیابی دستی لازم است. بکاپ: $BACKUP" >&2
  exit 1
}

rollback() {
  local reason="$1"
  echo "✗ $reason — بازگردانی ایمن نسخه $PREV"
  if [ "$DEPLOY_STARTED" = "0" ]; then
    git reset --hard "$PREV" || manual_recovery "بازگردانی کد ناموفق بود"
    status "failed" "$reason؛ کد قبلی حفظ شد، دیتابیس تغییر نکرد"
    exit 1
  fi

  # After migrations might have run, never restore over a live database.
  docker compose stop api worker || manual_recovery "توقف نویسندگان دیتابیس ناموفق بود"
  docker compose exec -T postgres pg_restore --list < "$BACKUP" > /dev/null ||
    manual_recovery "ساختار بکاپ نامعتبر است"

  # Restore into a NEW database first. Never destroy the production DB on failure.
  local staging="xagent_restore_${STAMP_ID}"
  local failed="xagent_failed_${STAMP_ID}"
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres \
    -c "CREATE DATABASE $staging OWNER xagent" ||
    manual_recovery "ایجاد دیتابیس بازیابی ناموفق بود؛ فضای دیسک را بررسی کنید"
  docker compose exec -T postgres pg_restore --exit-on-error --single-transaction \
    -U xagent -d "$staging" < "$BACKUP" ||
    manual_recovery "بازگردانی بکاپ به دیتابیس موقت شکست خورد؛ دیتابیس اصلی دست‌نخورده است"

  # Writer processes remain stopped. Rename is atomic for each database.
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'xagent' AND pid <> pg_backend_pid()" ||
    manual_recovery "قطع اتصالات دیتابیس ناموفق بود"
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres \
    -c "ALTER DATABASE xagent RENAME TO $failed" ||
    manual_recovery "تغییر نام دیتابیس فعلی ناموفق بود"
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres \
    -c "ALTER DATABASE $staging RENAME TO xagent" ||
    manual_recovery "تغییر نام دیتابیس بازیابی ناموفق بود؛ نسخه قبلی با نام $failed محفوظ است"

  git reset --hard "$PREV" || manual_recovery "بازگردانی کد ناموفق بود"
  docker compose build || manual_recovery "ساخت نسخه قبلی شکست خورد"
  docker compose up -d --remove-orphans || manual_recovery "راه‌اندازی نسخه قبلی شکست خورد"
  for i in $(seq 1 30); do
    if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" 2>/dev/null; then
      status "failed" "$reason؛ نسخه قبلی و دیتابیس با موفقیت بازیابی شدند"
      echo "✓ نسخه قبلی بازیابی شد. دیتابیس تغییر یافته تحت نام $failed برای بررسی باقی مانده است."
      exit 1
    fi
    sleep 3
  done
  manual_recovery "نسخه قبلی پس از بازیابی سالم نشد"
}

echo "→ ساخت ایمیج‌ها…"
docker compose build || rollback "ساخت ایمیج ناموفق بود"
echo "→ اجرای سرویس‌ها…"
DEPLOY_STARTED=1
docker compose up -d --remove-orphans || rollback "اجرای سرویس‌ها ناموفق بود"

echo "→ بررسی سلامت…"
for i in $(seq 1 40); do
  if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" 2>/dev/null; then
    printf '{"version":"%s","commit":"%s","updated_at":"%s"}\n' "$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)" "$NEW" "$(date -u +%FT%TZ)" > "$CTL/version.json"
    docker image prune -f >/dev/null 2>&1 || true
    echo "✓ به‌روزرسانی موفق: $PREV → $NEW"
    status "done" "به‌روزرسانی به $NEW انجام شد"
    exit 0
  fi
  sleep 3
done
rollback "سرویس پس از به‌روزرسانی سالم نشد"
