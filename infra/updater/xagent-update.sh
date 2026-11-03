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
if docker compose exec -T postgres pg_dump -U xagent -d xagent -Fc > "$XAGENT_HOME/backups/xagent-$STAMP.dump"; then
  echo "  ✓ $XAGENT_HOME/backups/xagent-$STAMP.dump"
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

rollback() {
  echo "✗ $1 — بازگشت به نسخه قبلی $PREV"
  git reset --hard "$PREV"
  docker compose build && docker compose up -d --remove-orphans
  status "failed" "$1؛ نسخه قبلی بازگردانده شد"
  exit 1
}

echo "→ ساخت ایمیج‌ها…"
docker compose build || rollback "ساخت ایمیج ناموفق بود"
echo "→ اجرای سرویس‌ها…"
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
