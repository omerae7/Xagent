#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
#  مهاجرت یک‌باره نصب‌های قدیمی X Agent به سیستم به‌روزرسانی جدید
#  (برای سرورهایی که دستور xagent ندارند یا از نسخه قدیمی به‌روزرسان استفاده می‌کنند)
#
#  curl -fsSL https://raw.githubusercontent.com/omerae7/Xagent/main/infra/updater/bootstrap.sh | sudo bash -s -- --ref main
#
#  کارها: بررسی نصب موجود ← دریافت امن ابزارهای مدیریتی از نسخه هدف (بدون تغییر پوشه برنامه) ←
#  نصب در /usr/local/lib/xagent و /usr/local/bin/xagent ← به‌روزرسانی سرویس systemd ← اجرای «xagent update».
# ════════════════════════════════════════════════════════════════════
set -euo pipefail
XAGENT_HOME="/opt/xagent"
REF=""
RUN_UPDATE=1
SET_BRANCH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --dir) XAGENT_HOME="$2"; shift 2 ;;
    --no-update) RUN_UPDATE=0; shift ;;
    --set-branch) SET_BRANCH=1; shift ;;
    *) echo "گزینه ناشناخته: $1"; exit 2 ;;
  esac
done
[ "$(id -u)" = "0" ] || { echo "با sudo اجرا کنید."; exit 1; }
APP="$XAGENT_HOME/app"
LIB="${XAGENT_LIB:-/usr/local/lib/xagent}"
BIN="${XAGENT_BIN:-/usr/local/bin}"
[ -d "$APP/.git" ] && [ -f "$APP/.env" ] || { echo "نصب X Agent در $XAGENT_HOME یافت نشد؛ برای نصب تازه از install.sh استفاده کنید."; exit 1; }
cd "$APP"
envget() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
REPO_URL="$(envget XAGENT_REPO_URL)"
ORIGIN="$(git remote get-url origin)"
if [ -n "$REPO_URL" ] && [ "${ORIGIN%.git}" != "${REPO_URL%.git}" ]; then
  echo "منبع مخزن ($ORIGIN) با پیکربندی ($REPO_URL) یکسان نیست؛ متوقف شد."; exit 1
fi
REF="${REF:-$(envget XAGENT_REPO_BRANCH)}"; REF="${REF:-main}"
echo "▶ دریافت ابزارهای مدیریتی از $ORIGIN ($REF)"
git fetch -q origin "+refs/heads/*:refs/remotes/origin/*" --tags
SRC="$(git rev-parse --verify -q "origin/$REF^{commit}" || git rev-parse --verify -q "$REF^{commit}")" || { echo "نسخه $REF یافت نشد"; exit 1; }
mkdir -p "$LIB"
git show "$SRC:infra/scripts/xagentctl" > "$LIB/xagentctl.new"
git show "$SRC:infra/updater/xagent-update.sh" > "$LIB/xagent-update.new"
bash -n "$LIB/xagentctl.new" && bash -n "$LIB/xagent-update.new" || { echo "اسکریپت‌های دریافتی معتبر نیستند"; exit 1; }
install -m 0755 "$LIB/xagentctl.new" "$LIB/xagentctl"
install -m 0755 "$LIB/xagent-update.new" "$LIB/xagent-update"
rm -f "$LIB"/*.new
ln -sf "$LIB/xagentctl" "$BIN/xagent"
printf 'XAGENT_HOME=%s\nXAGENT_LIB=%s\n' "$XAGENT_HOME" "$LIB" > "${XAGENT_CONF:-/etc/xagent.conf}"
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null; then
  git show "$SRC:infra/updater/xagent-updater.path" | sed "s#/opt/xagent#$XAGENT_HOME#g" > /etc/systemd/system/xagent-updater.path
  git show "$SRC:infra/updater/xagent-updater.service" | sed "s#/opt/xagent#$XAGENT_HOME#g; s#/usr/local/lib/xagent#$LIB#g" > /etc/systemd/system/xagent-updater.service
  systemctl daemon-reload
  systemctl enable --now xagent-updater.path >/dev/null 2>&1 || true
  echo "✓ سرویس به‌روزرسانی از پنل به مسیر ثابت منتقل شد."
else
  echo "⚠ systemd یافت نشد؛ به‌روزرسانی از پنل فعال نیست (دستور xagent update کار می‌کند)."
fi
# کارگزار فرمان میزبان از نسخه هدف (اگر آن نسخه آن را دارد)
if git cat-file -e "$SRC:infra/hostd/xagent-hostd.py" 2>/dev/null; then
  TMPH="$(mktemp -d)"
  for f in xagent-hostd.py xagent-hostd.socket xagent-hostd.service install-hostd.sh; do git show "$SRC:infra/hostd/$f" > "$TMPH/$f"; done
  bash "$TMPH/install-hostd.sh" "$XAGENT_HOME" "$LIB" "$TMPH" || echo "⚠ نصب کارگزار فرمان میزبان ناموفق بود"
  rm -rf "$TMPH"
fi
if [ "$SET_BRANCH" = "1" ]; then
  if grep -q '^XAGENT_REPO_BRANCH=' .env; then sed -i "s#^XAGENT_REPO_BRANCH=.*#XAGENT_REPO_BRANCH=$REF#" .env; else echo "XAGENT_REPO_BRANCH=$REF" >> .env; fi
fi
echo "✓ دستور «xagent» در مسیر ثابت نصب شد ($LIB)."
if [ "$RUN_UPDATE" = "1" ]; then
  exec "$LIB/xagentctl" update --ref "$REF" --yes
fi
