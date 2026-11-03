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

mkdir -p "$CTL" "$BK"
LOG="$CTL/update.log"
: > "$LOG"
chown --reference="$CTL" "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

c_g='\033[1;32m'; c_y='\033[1;33m'; c_r='\033[1;31m'; c_b='\033[1;34m'; c_o='\033[0m'
step() { echo -e "\n${c_b}▶ $*${c_o}"; }
ok()   { echo -e "${c_g}✓ $*${c_o}"; }
warn() { echo -e "${c_y}⚠ $*${c_o}"; }
status() {
  local msg="${2:-}"; msg="${msg//\"/\'}"
  printf '{"state":"%s","at":"%s","message":"%s","previous":"%s","target":"%s"}\n' \
    "$1" "$(date -u +%FT%TZ)" "$msg" "${PREV_SHORT:-}" "${NEW_SHORT:-}" > "$CTL/update.status"
  fixown "$CTL/update.status"
}
# فایل‌های کنترل باید برای API (کاربر غیر root داخل کانتینر) قابل نوشتن بمانند
fixown() { chown --reference="$CTL" "$@" 2>/dev/null || true; }
die() { echo -e "${c_r}✗ $1${c_o}" >&2; status "failed" "$1"; cleanup_worktree; exit 1; }

# ───────────── ۱. قفل ─────────────
exec 9>"$CTL/update.lock"
if ! flock -n 9; then echo "به‌روزرسانی دیگری در حال اجراست."; exit 3; fi
rm -f "$CTL/update.request"

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
  [ -n "$hb" ] && [ "$hb" != "(nil)" ]
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
  if dc_app exec -T web wget -q -O /dev/null --no-check-certificate https://localhost/ 2>/dev/null \
     || curl -fsSk -o /dev/null https://127.0.0.1/ 2>/dev/null; then ok "پنل وب پاسخ می‌دهد"; else warn "پاسخ پنل وب تأیید نشد (ممکن است به‌خاطر دامنه باشد)"; fi
  return 0
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
if [ "$DRY" = "1" ]; then ok "اجرای آزمایشی (--dry-run): تغییری اعمال نشد"; status "done" "dry-run"; exit 0; fi
if [ "$YES" != "1" ] && [ -t 0 ]; then
  read -rp "ادامه به‌روزرسانی؟ [y/N] " a; [ "$a" = "y" ] || { status "cancelled" "لغو توسط کاربر"; exit 0; }
fi

# ───────────── ۴. حفظ تغییرات محلی ─────────────
step "بررسی تغییرات محلی"
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  PATCH="$BK/local-changes-$STAMP.patch"
  { git diff HEAD; git ls-files --others --exclude-standard | sed 's/^/# untracked: /'; } > "$PATCH"
  git stash push --include-untracked -m "xagent-update-$STAMP" >/dev/null || die "حفظ تغییرات محلی ناموفق بود"
  warn "تغییرات محلی در $PATCH و git stash («xagent-update-$STAMP») حفظ شدند"
else
  ok "تغییر محلی وجود ندارد"
fi

# ───────────── ۵. پشتیبان ─────────────
step "پشتیبان‌گیری از دیتابیس و پیکربندی"
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
if ! dc_app exec -T postgres pg_restore --exit-on-error -U xagent -d "$CHECK_DB" < "$DB_BACKUP" >/dev/null; then
  drop_check; die "بازیابی کپی دیتابیس ناموفق بود"
fi
PGPASS="$(envget POSTGRES_PASSWORD)"
if ! dc_new run --rm --no-deps -e "XAGENT_DATABASE_URL=postgresql+asyncpg://xagent:${PGPASS}@postgres:5432/$CHECK_DB" api migrate; then
  drop_check; die "Migration نسخه جدید روی کپی دیتابیس شکست خورد؛ دیتابیس اصلی دست‌نخورده است"
fi
NEW_REV="$(alembic_rev "$CHECK_DB")"
drop_check
ok "Migration روی کپی موفق بود (${PREV_REV:-?} → ${NEW_REV:-?})"

# ───────────── ۱۰. استقرار ─────────────
step "استقرار نسخه جدید"
status "running" "استقرار"
envset XAGENT_IMAGE_TAG "$NEW_SHORT"
envset XAGENT_PREVIOUS_IMAGE_TAG "$PREV_TAG"

rollback() {
  local reason="$1"
  echo -e "${c_r}✗ $reason — بازگشت خودکار به نسخه $PREV_SHORT${c_o}"
  status "rolling_back" "$reason"
  envset XAGENT_IMAGE_TAG "$PREV_TAG"
  local cur_rev; cur_rev="$(alembic_rev)"
  if [ -n "$cur_rev" ] && [ "$cur_rev" != "$PREV_REV" ]; then
    echo "  Migration اجرا شده بود؛ بازیابی مرحله‌ای دیتابیس از پشتیبان (دیتابیس فعلی حذف نمی‌شود)…"
    dc_app stop api worker >/dev/null 2>&1
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
  if health_all; then
    status "rolled_back" "$reason؛ نسخه قبلی ($PREV_SHORT) با موفقیت بازگردانده شد"
    echo -e "${c_y}↩ نسخه قبلی بازگردانده شد. گزارش: $LOG${c_o}"
  else
    status "recovery_required" "$reason؛ نسخه قبلی هم سالم نشد. پشتیبان: $DB_BACKUP"
    echo -e "${c_r}بازیابی دستی لازم است: xagent logs api — پشتیبان: $DB_BACKUP${c_o}"
  fi
  cleanup_worktree
  exit 1
}

dc_app exec -T redis redis-cli DEL xagent:worker:heartbeat >/dev/null 2>&1 || true
dc_new up -d --remove-orphans || rollback "اجرای سرویس‌های جدید ناموفق بود"

# ───────────── ۱۰ب. سلامت ─────────────
step "بررسی سلامت نسخه جدید"
health_all || rollback "نسخه جدید آزمون سلامت را رد کرد"
CUR_REV="$(alembic_rev)"
[ -z "$NEW_REV" ] || [ "$CUR_REV" = "$NEW_REV" ] || rollback "نسخه Schema دیتابیس با انتظار یکسان نیست ($CUR_REV ≠ $NEW_REV)"
ok "Schema دیتابیس: $CUR_REV"

# ───────────── ۱۱. تثبیت ─────────────
step "تثبیت نسخه"
git checkout -q -B "$BRANCH" "$TARGET" 2>/dev/null || git checkout -q --detach "$TARGET"
if [ -n "${PATCH:-}" ]; then
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
fi
printf '{"version":"%s","commit":"%s","image_tag":"%s","previous_version":"%s","previous_commit":"%s","previous_image_tag":"%s","updated_at":"%s"}\n' \
  "$NEW_VERSION" "$TARGET" "$NEW_SHORT" "$PREV_VERSION" "$PREV" "$PREV_TAG" "$(date -u +%FT%TZ)" > "$CTL/version.json"
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
