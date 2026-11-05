#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  X Agent — به‌روزرسان امن یک‌دستوری (xagent update)
#
#  مراحل:
#   ۱. قفل و تشخیص نسخه نصب‌شده/منبع معتبر         ۷. ساخت ایمیج‌ها با تگ نسخه (نسخه قبلی دست‌نخورده)
#   ۲. پیش‌بررسی (Docker، دیسک، سلامت، مخزن)       ۸. آزمون دود ایمیج جدید
#   ۳. دریافت نسخه هدف و بررسی Fast-Forward         ۹. آزمون Migration روی «کپی» دیتابیس
#   ۴. حفظ تغییرات محلی (patch + stash)              ۱۰. استقرار و بررسی سلامت API/Worker/DB/پنل
#   ۵. پشتیبان دیتابیس و فایل‌های حیاتی              ۱۱. شکست ← بازگشت به تگ ایمیج و (در صورت لزوم) دیتابیس قبلی
#   ۶. ساخت worktree جدا برای نسخه هدف               ۱۲. گزارش نسخه قبلی/جدید و نتیجه آزمون‌ها
#
#  گزینه‌ها: --ref <branch|tag|commit>  --yes  --dry-run  --allow-downgrade  --force (ادامه با سرویس ناسالم)
# ════════════════════════════════════════════════════════════════════════════
set -uo pipefail

XAGENT_HOME="${XAGENT_HOME:-/opt/xagent}"
APP="$XAGENT_HOME/app"
CTL="$XAGENT_HOME/data/control"
BK="$XAGENT_HOME/backups"
LIB="${XAGENT_LIB:-/usr/local/lib/xagent}"
MIN_FREE_GB="${XAGENT_MIN_FREE_GB:-4}"
HEALTH_TRIES="${XAGENT_HEALTH_TRIES:-60}"
REF=""
YES=0
DRY=0
ALLOW_DOWNGRADE=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --allow-downgrade) ALLOW_DOWNGRADE=1; shift ;;
    --force) FORCE=1; shift ;;
    *) echo "گزینه ناشناخته: $1"; exit 2 ;;
  esac
done

# Dry-run is non-deploying: do not overwrite update.status/update.log,
# consume update.request, or create backup/control directories.
# Git fetch further below DOES refresh local Git remote-tracking metadata.
if [ "$DRY" = "1" ]; then
  if [ -f "$CTL/update.lock" ]; then
    exec 9< "$CTL/update.lock"
    if ! flock -sn 9; then echo "به‌روزرسانی دیگری در حال اجراست."; exit 3; fi
  fi
else
  mkdir -p "$CTL" "$BK"
  # ───────────── ۱. قفل (پیش از هر نوشتن روی گزارش/وضعیت) ─────────────
  exec 9>"$CTL/update.lock"
  if ! flock -n 9; then echo "به‌روزرسانی دیگری در حال اجراست."; exit 3; fi
  rm -f "$CTL/update.request"
  LOG="$CTL/update.log"
  : > "$LOG"
  chown --reference="$CTL" "$LOG" 2>/dev/null || true
  exec > >(tee -a "$LOG") 2>&1
fi

c_g='\033[1;32m'; c_y='\033[1;33m'; c_r='\033[1;31m'; c_b='\033[1;34m'; c_o='\033[0m'
step() { echo -e "\n${c_b}▶ $*${c_o}"; }
ok()   { echo -e "${c_g}✓ $*${c_o}"; }
warn() { echo -e "${c_y}⚠ $*${c_o}"; }
status() {
  # Dry-run is a query; it must not replace the status of a real update.
  if [ "$DRY" = "1" ]; then return 0; fi
  local msg="${2:-}"; msg="${msg//\"/\'}"
  printf '{"state":"%s","at":"%s","message":"%s","previous":"%s","target":"%s"}\n' \
    "$1" "$(date -u +%FT%TZ)" "$msg" "${PREV_SHORT:-}" "${NEW_SHORT:-}" > "$CTL/update.status"
  fixown "$CTL/update.status"
}
# فایل‌های کنترل باید برای API (کاربر غیر root داخل کانتینر) قابل نوشتن بمانند
fixown() { chown --reference="$CTL" "$@" 2>/dev/null || true; }
QUIESCED=0
DEPLOYED=0
die() {
  echo -e "${c_r}✗ $1${c_o}" >&2
  if [ "$QUIESCED" = "1" ] && [ "$DEPLOYED" = "0" ]; then
    echo "  پنجره نگهداری بسته می‌شود؛ سرویس‌های نسخه فعلی دوباره اجرا می‌شوند…"
    dc_app up -d >/dev/null 2>&1 || true
  fi
  status "failed" "$1"; cleanup_worktree; exit 1
}


[ -d "$APP/.git" ] || { echo "مخزن برنامه در $APP یافت نشد."; exit 1; }
[ -f "$APP/.env" ] || { echo "فایل پیکربندی $APP/.env یافت نشد."; exit 1; }
cd "$APP"
envget() { grep -E "^$1=" "$APP/.env" | tail -1 | cut -d= -f2-; }
envset() {
  if grep -qE "^$1=" "$APP/.env"; then sed -i "s#^$1=.*#$1=$2#" "$APP/.env"; else echo "$1=$2" >> "$APP/.env"; fi
}

PREV="$(git rev-parse HEAD)"; PREV_SHORT="${PREV:0:12}"
PREV_TAG="$(envget XAGENT_IMAGE_TAG)"; PREV_TAG="${PREV_TAG:-latest}"
PREV_VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
REPO_URL="$(envget XAGENT_REPO_URL)"
BRANCH="$(envget XAGENT_REPO_BRANCH)"; BRANCH="${BRANCH:-main}"
REF="${REF:-$BRANCH}"
STAMP="$(date +%Y%m%d-%H%M%S)"
WT=""

cleanup_worktree() {
  if [ -n "${WT:-}" ] && [ -d "$WT" ]; then git -C "$APP" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"; fi
}
dc_app() { docker compose --project-directory "$APP" -f "$APP/compose.yaml" --env-file "$APP/.env" "$@"; }

health_api() {
  dc_app exec -T api python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)" >/dev/null 2>&1
}
health_worker() {
  # Worker هر ۳۰ ثانیه ضربان قلب را در Redis می‌نویسد
  local hb; hb="$(dc_app exec -T redis redis-cli GET xagent:worker:heartbeat 2>/dev/null | tr -d '\r')"
  [ -n "$hb" ] && [ "$hb" != "(nil)" ] || return 1
  # آمادگی عملکردی: حلقه‌های ضروری (صف، رویداد، کنترل، زمان‌بند) زنده‌اند — ضربان قلب به‌تنهایی کافی نیست.
  # نسخه‌های قدیمی گزارش آمادگی ندارند: برای آن‌ها فقط ضربان قلب (رفتار قبلی) بررسی می‌شود.
  local rd; rd="$(dc_app exec -T redis redis-cli GET xagent:worker:readiness 2>/dev/null | tr -d '\r')"
  if [ -z "$rd" ] || [ "$rd" = "(nil)" ]; then return 0; fi
  printf '%s' "$rd" | grep -q '"ready": true'
}
health_all() {
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    if health_api; then break; fi
    sleep 3
  done
  health_api || { echo "  API سالم نشد"; return 1; }
  ok "API و دیتابیس و Redis سالم‌اند"
  for i in $(seq 1 "$HEALTH_TRIES"); do
    if health_worker; then ok "Worker فعال است (ضربان قلب دریافت شد)"; break; fi
    sleep 3
  done
  health_worker || { echo "  Worker ضربان قلب نفرستاد"; return 1; }
  health_web "${1:-$APP}" || return 1
  return 0
}
# پنل وب: صفحه اصلی پنل و پروکسی API از درون Caddy روی پورت سلامت داخلی (منتشر نمی‌شود)
web_strict() { grep -q ':8088' "$1/infra/caddy/Caddyfile" 2>/dev/null; }
health_web() {
  local dir="$1" i
  if ! web_strict "$dir"; then
    # نسخه‌های قدیمی بدون پورت سلامت: بررسی سازگار (فقط هشدار)
    if dc_app exec -T web wget -q -O /dev/null --no-check-certificate https://localhost/ 2>/dev/null; then ok "پنل وب پاسخ می‌دهد"
    else warn "نسخه قدیمی پورت سلامت پنل ندارد؛ پاسخ پنل تأیید نشد"; fi
    return 0
  fi
  for i in $(seq 1 "$HEALTH_TRIES"); do
    if dc_app exec -T web wget -q -O - http://127.0.0.1:8088/ 2>/dev/null | grep -q 'id="root"' \
       && dc_app exec -T web wget -q -O - http://127.0.0.1:8088/api/health 2>/dev/null | grep -q '"ok":true'; then
      ok "پنل وب و پروکسی API سالم‌اند"; return 0
    fi
    sleep 3
  done
  echo "  پنل وب سالم نیست (صفحه پنل یا مسیر /api از طریق Caddy پاسخ نداد)"; return 1
}
alembic_rev() {
  dc_app exec -T postgres psql -U xagent -d "${1:-xagent}" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null | tr -d '[:space:]'
}

echo "══ به‌روزرسانی X Agent — $(date) ══"
status "running" "پیش‌بررسی"

# ───────────── ۲. پیش‌بررسی ─────────────
step "پیش‌بررسی سرور"
command -v docker >/dev/null || die "Docker نصب نیست"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 در دسترس نیست"
FREE_GB="$(df -BG --output=avail "$XAGENT_HOME" | tail -1 | tr -dc '0-9')"
[ "${FREE_GB:-0}" -ge "$MIN_FREE_GB" ] || die "فضای آزاد دیسک ${FREE_GB}GB است؛ حداقل ${MIN_FREE_GB}GB لازم است. ابتدا فضا آزاد کنید (docker image prune)."
ok "فضای آزاد: ${FREE_GB}GB"
if health_api; then ok "سرویس فعلی سالم است ($PREV_VERSION / ${PREV_SHORT})"
elif [ "$FORCE" = "1" ]; then warn "سرویس فعلی سالم نیست؛ با --force ادامه می‌دهیم"
else die "سرویس فعلی سالم نیست؛ برای جلوگیری از از دست رفتن مسیر بازگشت متوقف شد. (xagent status / --force)"; fi
ORIGIN="$(git remote get-url origin)"
if [ -n "$REPO_URL" ] && [ "${ORIGIN%.git}" != "${REPO_URL%.git}" ]; then
  die "منبع مخزن ($ORIGIN) با منبع معتبر پیکربندی ($REPO_URL) یکسان نیست؛ به‌روزرسانی انجام نشد."
fi
ok "منبع معتبر: ${ORIGIN} (شاخه/نسخه: $REF)"

# ───────────── ۳. دریافت نسخه هدف ─────────────
step "دریافت نسخه هدف"
git fetch --tags --prune origin "+refs/heads/*:refs/remotes/origin/*" >/dev/null 2>&1 || die "دریافت از مخزن ناموفق بود (اتصال اینترنت/GitHub را بررسی کنید)"
TARGET="$(git rev-parse --verify -q "origin/$REF^{commit}" || git rev-parse --verify -q "$REF^{commit}")" || die "نسخه «$REF» در مخزن یافت نشد"
NEW_SHORT="${TARGET:0:12}"
NEW_VERSION="$(git show "$TARGET:pyproject.toml" | grep -m1 '^version' | cut -d'"' -f2)"
if [ "$TARGET" = "$PREV" ]; then ok "نسخه نصب‌شده به‌روز است ($PREV_VERSION / $PREV_SHORT)"; status "done" "نسخه فعلی به‌روز است"; exit 0; fi
if ! git merge-base --is-ancestor "$PREV" "$TARGET"; then
  [ "$ALLOW_DOWNGRADE" = "1" ] || die "نسخه هدف ادامه مستقیم نسخه فعلی نیست (Downgrade یا شاخه موازی). در صورت اطمینان: --allow-downgrade"
  warn "نسخه هدف ادامه مستقیم نیست (اجازه داده شده)"
fi
echo "  فعلی: $PREV_VERSION ($PREV_SHORT)  ←  هدف: $NEW_VERSION ($NEW_SHORT)"
git --no-pager log --oneline "$PREV..$TARGET" 2>/dev/null | head -20 | sed 's/^/    /'
if [ "$DRY" = "1" ]; then ok "اجرای آزمایشی: هیچ سرویس، دیتابیس، تنظیم یا فایل وضعیت برنامه تغییر نکرد؛ فقط اطلاعات Git fetch به‌روز شد"; exit 0; fi
if [ "$YES" != "1" ] && [ -t 0 ]; then
  read -rp "ادامه به‌روزرسانی؟ [y/N] " a; [ "$a" = "y" ] || { status "cancelled" "لغو توسط کاربر"; exit 0; }
fi

# ───────────── ۴. تغییرات محلی ─────────────
step "بررسی تغییرات محلی"
LOCAL_CHANGES=0
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  LOCAL_CHANGES=1
  PATCH="$BK/local-changes-$STAMP.patch"
  { git diff HEAD; git ls-files --others --exclude-standard | sed 's/^/# untracked: /'; } > "$PATCH"
  chmod 600 "$PATCH"
  warn "تغییرات محلی در $PATCH ثبت شد؛ پوشه برنامه تا مرحله تثبیت دست‌نخورده می‌ماند"
else
  ok "تغییر محلی وجود ندارد"
fi
PREV_REV="$(alembic_rev)"

# ───────────── ۶. worktree جدا ─────────────
step "آماده‌سازی نسخه هدف در پوشه جدا"
WT="$XAGENT_HOME/releases/$NEW_SHORT"
rm -rf "$WT"; mkdir -p "$XAGENT_HOME/releases"
git worktree prune >/dev/null 2>&1 || true
git worktree add --detach "$WT" "$TARGET" >/dev/null 2>&1 || die "ساخت پوشه نسخه هدف ناموفق بود"
ln -sf "$APP/.env" "$WT/.env"
dc_new() { XAGENT_IMAGE_TAG="$NEW_SHORT" docker compose --project-directory "$WT" -f "$WT/compose.yaml" --env-file "$APP/.env" "$@"; }

# ───────────── ۷. ساخت ─────────────
step "ساخت ایمیج‌های نسخه $NEW_SHORT (سرویس‌های فعلی دست‌نخورده‌اند)"
dc_new build || die "ساخت ایمیج‌ها ناموفق بود؛ سرویس فعلی بدون تغییر ادامه می‌دهد"
ok "ایمیج‌ها ساخته شدند"

# ───────────── ۸. آزمون دود ─────────────
step "آزمون دود ایمیج جدید"
docker run --rm --entrypoint python "xagent/backend:$NEW_SHORT" -c "import xagent_api.main, xagent_worker.main, xagent_agent.pipeline; print('ok')" \
  || die "ایمیج جدید بارگذاری نشد؛ سرویس فعلی بدون تغییر ادامه می‌دهد"
ok "ماژول‌های API و Worker بارگذاری شدند"

# ───────────── ۹. آزمون Migration روی کپی ─────────────
step "آزمون Migration روی کپی دیتابیس"
CHECK_DB="xagent_migcheck_${STAMP//-/_}"
dc_app exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres -c "CREATE DATABASE $CHECK_DB OWNER xagent" >/dev/null \
  || die "ساخت دیتابیس آزمون ناموفق بود (فضای دیسک؟)"
drop_check() { dc_app exec -T postgres psql -U xagent -d postgres -c "DROP DATABASE IF EXISTS $CHECK_DB" >/dev/null 2>&1; }
PRECHECK="$BK/precheck-$STAMP.dump"
dc_app exec -T postgres pg_dump -U xagent -d xagent -Fc > "$PRECHECK" || { rm -f "$PRECHECK"; drop_check; die "تهیه کپی آزمایشی دیتابیس ناموفق بود"; }
chmod 600 "$PRECHECK"
if ! dc_app exec -T postgres pg_restore --exit-on-error -U xagent -d "$CHECK_DB" < "$PRECHECK" >/dev/null; then
  rm -f "$PRECHECK"; drop_check; die "بازیابی کپی دیتابیس ناموفق بود"
fi
rm -f "$PRECHECK"
PGPASS="$(envget POSTGRES_PASSWORD)"
if ! dc_new run --rm --no-deps -e "XAGENT_DATABASE_URL=postgresql+asyncpg://xagent:${PGPASS}@postgres:5432/$CHECK_DB" api migrate; then
  drop_check; die "Migration نسخه جدید روی کپی دیتابیس شکست خورد؛ دیتابیس اصلی دست‌نخورده است"
fi
NEW_REV="$(alembic_rev "$CHECK_DB")"
drop_check
ok "Migration روی کپی موفق بود (${PREV_REV:-?} → ${NEW_REV:-?})"

# ───────────── ۱۰. پنجره نگهداری و پشتیبان قطعی ─────────────
# نوشتن داده متوقف می‌شود تا پشتیبان دقیقاً آخرین وضعیت باشد و هیچ داده‌ای بین پشتیبان و استقرار از دست نرود
step "پنجره نگهداری: توقف نوشتن داده و پشتیبان‌گیری نهایی"
status "running" "پنجره نگهداری"
QUIESCED=1
dc_app stop worker api >/dev/null 2>&1 || die "توقف موقت API/Worker ناموفق بود"
ok "API و Worker موقتاً متوقف شدند (اجراهای در حال انجام از Checkpoint ادامه می‌یابند)"
DB_BACKUP="$BK/db-$STAMP.dump"
dc_app exec -T postgres pg_dump -U xagent -d xagent -Fc > "$DB_BACKUP" || die "پشتیبان‌گیری دیتابیس ناموفق بود"
dc_app exec -T postgres pg_restore --list < "$DB_BACKUP" >/dev/null || die "فایل پشتیبان دیتابیس معتبر نیست"
CFG_BACKUP="$BK/config-$STAMP.tar.gz"
tar -czf "$CFG_BACKUP" -C "$XAGENT_HOME" app/.env data/control 2>/dev/null || die "پشتیبان‌گیری پیکربندی ناموفق بود"
chmod 600 "$DB_BACKUP" "$CFG_BACKUP"
ok "دیتابیس: $DB_BACKUP ($(du -h "$DB_BACKUP" | cut -f1))"
ok "پیکربندی و اسرار: $CFG_BACKUP"
ls -1t "$BK"/db-*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$BK"/config-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
[ "$(alembic_rev)" = "$PREV_REV" ] || die "نسخه Schema در حین به‌روزرسانی تغییر کرد؛ متوقف شد"

# ───────────── ۱۱. استقرار ─────────────
step "استقرار نسخه جدید"
status "running" "استقرار"
OLD_PREVIOUS_TAG="$(envget XAGENT_PREVIOUS_IMAGE_TAG)"
envset XAGENT_IMAGE_TAG "$NEW_SHORT"
envset XAGENT_PREVIOUS_IMAGE_TAG "$PREV_TAG"

# >>> preserve_failed_release (F11)
# پیش از هر downgrade بالقوه مخرب: کپی کامل وضعیت نسخه ناموفق (شامل جدول/ستون‌های تازه و داده‌های پذیرفته‌شده
# پس از ارتقا) ذخیره و اعتبارسنجی می‌شود و جدول‌های تازه دارای ردیف با شمار ردیف گزارش می‌شوند. بدون این کپی
# downgrade انجام نمی‌شود. فقط از pg_dump/psql/pg_restore داخل کانتینر Postgres (dc_app) استفاده می‌کند.
preserve_failed_release() {
  local out="$1" before="$2" manifest="$1.manifest.txt"
  dc_app exec -T postgres pg_dump -U xagent -d xagent -Fc > "$out" 2>/dev/null || { rm -f "$out"; return 1; }
  chmod 600 "$out" 2>/dev/null || true
  dc_app exec -T postgres pg_restore --list < "$out" >/dev/null 2>&1 || { rm -f "$out"; return 1; }
  : > "$manifest"
  local old_tables="" t n
  [ -f "$before" ] && old_tables="$(dc_app exec -T postgres pg_restore --list < "$before" 2>/dev/null \
    | awk '$4=="TABLE" && $5=="public" {print $6}' | sort -u)"
  for t in $(dc_app exec -T postgres psql -U xagent -d xagent -tAc \
      "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1" 2>/dev/null); do
    if [ -n "$old_tables" ] && ! grep -qx "$t" <<<"$old_tables"; then
      n="$(dc_app exec -T postgres psql -U xagent -d xagent -tAc "SELECT count(*) FROM \"$t\"" 2>/dev/null | tr -d '[:space:]')"
      echo "new_table $t rows=${n:-?}" >> "$manifest"
    fi
  done
  chmod 600 "$manifest" 2>/dev/null || true
  return 0
}
# <<< preserve_failed_release

rollback() {
  local reason="$1"
  echo -e "${c_r}✗ $reason — بازگشت خودکار به نسخه $PREV_SHORT${c_o}"
  status "rolling_back" "$reason"
  envset XAGENT_IMAGE_TAG "$PREV_TAG"
  envset XAGENT_PREVIOUS_IMAGE_TAG "${OLD_PREVIOUS_TAG:-$PREV_TAG}"
  local cur_rev; cur_rev="$(alembic_rev)"
  if [ -n "$cur_rev" ] && [ "$cur_rev" != "$PREV_REV" ]; then
    dc_app stop api worker >/dev/null 2>&1
    local post="$BK/failed-release-$STAMP.dump"
    if preserve_failed_release "$post" "$DB_BACKUP"; then
      echo "  کپی کامل وضعیت نسخه ناموفق پیش از downgrade: $post"
      [ -s "$post.manifest.txt" ] && echo "  جدول‌های تازه نسخه ناموفق (داده در کپی بالا حفظ است): $(tr '\n' ' ' < "$post.manifest.txt")"
      echo "  Migration اجرا شده بود؛ بازگرداندن Schema با downgrade…"
      if dc_new run --rm --no-deps -w /app/services/api --entrypoint alembic api downgrade "$PREV_REV" >/dev/null 2>&1 \
         && [ "$(alembic_rev)" = "$PREV_REV" ]; then
        ok "Schema با downgrade به $PREV_REV برگشت؛ داده جداول قبلی در دیتابیس ماند و جدول/ستون‌های حذف‌شده نسخه ناموفق در $post قابل بازیابی‌اند"
        cur_rev="$PREV_REV"
      else
        echo "  downgrade ممکن نبود؛ بازیابی مرحله‌ای دیتابیس از پشتیبان پنجره نگهداری (دیتابیس فعلی حذف نمی‌شود)…"
      fi
    else
      # بدون کپی معتبر از داده‌های پس از ارتقا، downgrade مخرب انجام نمی‌شود؛ مسیر غیرمخرب (نگه‌داشتن دیتابیس فعلی با نام دیگر)
      echo "  کپی وضعیت نسخه ناموفق ممکن نشد؛ downgrade انجام نمی‌شود و دیتابیس فعلی با نام دیگر نگه داشته می‌شود…"
    fi
  fi
  if [ -n "$cur_rev" ] && [ "$cur_rev" != "$PREV_REV" ]; then
    local staging="xagent_restore_${STAMP//-/_}" failed="xagent_failed_${STAMP//-/_}"
    dc_app exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres -c "CREATE DATABASE $staging OWNER xagent" >/dev/null &&
    dc_app exec -T postgres pg_restore --exit-on-error -U xagent -d "$staging" < "$DB_BACKUP" >/dev/null &&
    dc_app exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres -c \
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='xagent' AND pid<>pg_backend_pid()" >/dev/null &&
    dc_app exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres -c "ALTER DATABASE xagent RENAME TO $failed" >/dev/null &&
    dc_app exec -T postgres psql -v ON_ERROR_STOP=1 -U xagent -d postgres -c "ALTER DATABASE $staging RENAME TO xagent" >/dev/null || {
      status "recovery_required" "$reason؛ بازیابی دیتابیس کامل نشد. پشتیبان: $DB_BACKUP"
      echo -e "${c_r}بازیابی دستی لازم است. پشتیبان سالم: $DB_BACKUP — راهنما: xagent restore $DB_BACKUP${c_o}"
      cleanup_worktree; exit 1; }
    echo "  دیتابیس نسخه قبل بازیابی شد؛ دیتابیس تغییریافته با نام $failed برای بررسی نگه داشته شد."
  fi
  dc_app up -d --remove-orphans >/dev/null 2>&1
  if health_all "$APP"; then
    status "rolled_back" "$reason؛ نسخه قبلی ($PREV_SHORT) با موفقیت بازگردانده شد"
    echo -e "${c_y}↩ نسخه قبلی بازگردانده شد. گزارش: $LOG${c_o}"
  else
    status "recovery_required" "$reason؛ نسخه قبلی هم سالم نشد. پشتیبان: $DB_BACKUP"
    echo -e "${c_r}بازیابی دستی لازم است: xagent logs api — پشتیبان: $DB_BACKUP${c_o}"
  fi
  cleanup_worktree
  exit 1
}

dc_app exec -T redis redis-cli DEL xagent:worker:heartbeat xagent:worker:readiness >/dev/null 2>&1 || true
DEPLOYED=1
dc_new up -d --remove-orphans || rollback "اجرای سرویس‌های جدید ناموفق بود"

# ───────────── ۱۰ب. سلامت ─────────────
step "بررسی سلامت نسخه جدید"
health_all "$WT" || rollback "نسخه جدید آزمون سلامت را رد کرد"
CUR_REV="$(alembic_rev)"
[ -z "$NEW_REV" ] || [ "$CUR_REV" = "$NEW_REV" ] || rollback "نسخه Schema دیتابیس با انتظار یکسان نیست ($CUR_REV ≠ $NEW_REV)"
ok "Schema دیتابیس: $CUR_REV"

# ───────────── ۱۱. تثبیت ─────────────
step "تثبیت نسخه"
# اگر هدف یک شاخه دیگر بود (مثلاً مهاجرت از شاخه قدیمی به main)، منبع به‌روزرسانی‌های بعدی هم همان شاخه می‌شود
if [ "$REF" != "$BRANCH" ] && git show-ref --verify -q "refs/remotes/origin/$REF"; then
  envset XAGENT_REPO_BRANCH "$REF"
  ok "شاخه منبع به‌روزرسانی از «$BRANCH» به «$REF» تغییر کرد"
  BRANCH="$REF"
fi
STASHED=0
if [ "$LOCAL_CHANGES" = "1" ]; then
  if git -c user.name="X Agent Updater" -c user.email=updater@xagent.local stash push --include-untracked -m "xagent-update-$STAMP" >/dev/null; then
    STASHED=1
  else
    rollback "حفظ تغییرات محلی پیش از تثبیت ناموفق بود (تغییرات در $PATCH و پوشه برنامه باقی است)"
  fi
fi
if ! { git checkout -q -B "$BRANCH" "$TARGET" 2>/dev/null || git checkout -q --detach "$TARGET"; }; then
  if [ "$STASHED" = "1" ] && ! git -c user.name="X Agent Updater" -c user.email=updater@xagent.local stash pop -q 2>/dev/null; then
    rollback "تثبیت ناموفق بود و بازگرداندن خودکار تغییرات محلی هم ممکن نشد؛ تغییرات در stash «xagent-update-$STAMP» و فایل $PATCH حفظ‌اند (بازیابی: git -C $APP stash pop)"
  fi
  rollback "تثبیت نسخه در پوشه برنامه ناموفق بود؛ تغییرات محلی سر جای خود بازگردانده شدند"
fi
if [ "$STASHED" = "1" ]; then
  # فایل‌های ردیابی‌نشده اپراتور اگر با نسخه جدید تداخل ندارند برگردانده می‌شوند؛
  # تغییرات فایل‌های برنامه در stash/patch می‌مانند تا با کد جدید تداخل نکنند.
  SREF="$(git stash list --format='%gd %s' | awk -v m="xagent-update-$STAMP" '$0 ~ m {print $1; exit}')"
  if [ -n "$SREF" ] && git rev-parse -q --verify "$SREF^3" >/dev/null; then
    git ls-tree -r -z --name-only "$SREF^3" | while IFS= read -r -d '' f; do
      if [ ! -e "$f" ] && ! git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then
        mkdir -p "$(dirname "$f")" && git show "$SREF^3:$f" > "$f" && echo "  ↩ بازگردانده شد: $f"
      fi
    done
  fi
  warn "تغییرات فایل‌های برنامه در $PATCH مانده‌اند (اعمال دستی: git -C $APP stash apply \"stash^{/xagent-update-$STAMP}\")"
fi
cleanup_worktree
WT=""
dc_app up -d --remove-orphans >/dev/null 2>&1  # پروژه دوباره از پوشه اصلی مدیریت می‌شود (بدون ساخت مجدد)
# نصب مجدد ابزارهای ثابت از نسخه جدید (این اسکریپت از نسخه موقت اجرا می‌شود و تعویضش امن است)
if [ -w "$(dirname "$LIB")" ] || [ "$(id -u)" = "0" ]; then
  mkdir -p "$LIB"
  install -m 0755 "$APP/infra/scripts/xagentctl" "$LIB/xagentctl"
  install -m 0755 "$APP/infra/updater/xagent-update.sh" "$LIB/xagent-update"
  ln -sf "$LIB/xagentctl" "${XAGENT_BIN:-/usr/local/bin}/xagent" 2>/dev/null || true
  if [ -f "$APP/infra/hostd/install-hostd.sh" ] && [ -z "${XAGENT_SKIP_HOSTD:-}" ]; then
    bash "$APP/infra/hostd/install-hostd.sh" "$XAGENT_HOME" "$LIB" "$APP/infra/hostd" || warn "به‌روزرسانی کارگزار فرمان میزبان ناموفق بود"
  fi
fi
printf '{"version":"%s","commit":"%s","image_tag":"%s","previous_version":"%s","previous_commit":"%s","previous_image_tag":"%s","previous_db_revision":"%s","db_revision":"%s","db_backup":"%s","updated_at":"%s"}\n' \
  "$NEW_VERSION" "$TARGET" "$NEW_SHORT" "$PREV_VERSION" "$PREV" "$PREV_TAG" "$PREV_REV" "$CUR_REV" "$DB_BACKUP" "$(date -u +%FT%TZ)" > "$CTL/version.json"
fixown "$CTL/version.json"
# فقط ایمیج‌های دو نسخه اخیر نگه داشته می‌شوند (برای Rollback)
for img in backend web runner desktop; do
  docker images "xagent/$img" --format '{{.Tag}}' | grep -vxE "$NEW_SHORT|$PREV_TAG" | xargs -r -I{} docker rmi "xagent/$img:{}" >/dev/null 2>&1 || true
done
status "done" "به‌روزرسانی از $PREV_VERSION ($PREV_SHORT) به $NEW_VERSION ($NEW_SHORT) انجام شد"

echo
echo -e "${c_g}══════════════════════════════════════════════${c_o}"
echo -e "${c_g} ✅ به‌روزرسانی موفق${c_o}"
echo -e "   نسخه قبلی: $PREV_VERSION ($PREV_SHORT, ایمیج $PREV_TAG)"
echo -e "   نسخه جدید: $NEW_VERSION ($NEW_SHORT)"
echo -e "   آزمون‌ها: ساخت ✓ · دود ✓ · Migration روی کپی ✓ · سلامت API/Worker/DB ✓"
echo -e "   پشتیبان: $DB_BACKUP"
echo -e "   بازگشت در صورت نیاز: xagent rollback"
echo -e "   گزارش کامل: $LOG"
echo -e "${c_g}══════════════════════════════════════════════${c_o}"
exit 0
