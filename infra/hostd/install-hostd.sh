#!/usr/bin/env bash
# نصب/به‌روزرسانی کارگزار فرمان میزبان (xagent-hostd) — فقط root
#   install-hostd.sh <XAGENT_HOME> <LIB_DIR> <SOURCE_DIR(محتوای infra/hostd)>
# سوکت یونیکس در $XAGENT_HOME/run/hostd (root:1001، 0750) فقط برای کانتینر API (uid 1001) قابل دسترسی است؛ هیچ پورت شبکه‌ای باز نمی‌شود.
set -euo pipefail
HOME_DIR="$1"; LIB="$2"; SRC="$3"
[ "$(id -u)" = "0" ] || { echo "install-hostd: نیازمند root"; exit 1; }
command -v python3 >/dev/null || { echo "install-hostd: python3 یافت نشد؛ کارگزار میزبان نصب نشد"; exit 0; }
mkdir -p "$LIB" /etc/xagent "$HOME_DIR/run/hostd/jobs"
python3 -m py_compile "$SRC/xagent-hostd.py"
install -m 0755 "$SRC/xagent-hostd.py" "$LIB/xagent-hostd"
chown root:1001 "$HOME_DIR/run" "$HOME_DIR/run/hostd" 2>/dev/null || true
chmod 0755 "$HOME_DIR/run"
chmod 0750 "$HOME_DIR/run/hostd"
chown root:root "$HOME_DIR/run/hostd/jobs"; chmod 0755 "$HOME_DIR/run/hostd/jobs"
DOCKER="$(command -v docker || echo /usr/bin/docker)"
JOURNAL="$(command -v journalctl || echo /usr/bin/journalctl)"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
umask 077
cat > /etc/xagent/hostd.json <<JSON
{"home": "$HOME_DIR", "run_dir": "$HOME_DIR/run/hostd", "env_file": "$HOME_DIR/app/.env", "app_dir": "$HOME_DIR/app",
 "allowed_uids": [0, 1001], "max_concurrent": 3, "idle_exit_seconds": 600, "xagent_conf": "${XAGENT_CONF:-/etc/xagent.conf}",
 "audit_log": "/var/log/xagent-hostd.log", "socket_gid": 1001,
 "bin": {"docker": "$DOCKER", "xagentctl": "$LIB/xagentctl", "journalctl": "$JOURNAL", "systemctl": "$SYSTEMCTL"}}
JSON
umask 022
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null; then
  sed "s#/opt/xagent#$HOME_DIR#g" "$SRC/xagent-hostd.socket" > /etc/systemd/system/xagent-hostd.socket
  sed "s#/usr/local/lib/xagent#$LIB#g" "$SRC/xagent-hostd.service" > /etc/systemd/system/xagent-hostd.service
  systemctl daemon-reload
  systemctl enable --now xagent-hostd.socket >/dev/null 2>&1 || true
  # نسخه جدید در اتصال بعدی اجرا می‌شود (کارهای در حال اجرا در Session مستقل ادامه می‌یابند)
  systemctl try-restart xagent-hostd.service >/dev/null 2>&1 || true
  echo "✓ کارگزار فرمان میزبان نصب شد (سوکت: $HOME_DIR/run/hostd/hostd.sock، بدون پورت شبکه)"
else
  echo "⚠ systemd یافت نشد؛ کارگزار فرمان میزبان فعال نشد (اجرای دستی: python3 $LIB/xagent-hostd --config /etc/xagent/hostd.json)"
fi
