#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
#  X Agent — نصاب تک‌خطی
#  نصب روی Ubuntu 24.04 (و 22.04 / Debian 12) خام:
#
#    curl -fsSL https://raw.githubusercontent.com/omerae7/Xagent/main/install.sh | sudo bash
#
#  با دامنه و HTTPS معتبر:
#    curl -fsSL https://raw.githubusercontent.com/omerae7/Xagent/main/install.sh | sudo bash -s -- --domain agent.example.com --email you@example.com
#
#  گزینه‌ها:
#    --domain NAME        دامنه (گواهی Let's Encrypt خودکار). بدون دامنه: HTTPS با گواهی داخلی روی IP
#    --email EMAIL        ایمیل ACME
#    --profile P          lite | standard | performance (پیش‌فرض: تشخیص خودکار از سخت‌افزار)
#    --no-desktop         بدون کانتینر دسکتاپ (مناسب سرورهای کم‌حافظه)
#    --with-desktop       اجبار به نصب دسکتاپ حتی در پروفایل lite
#    --swap SIZE_GB       اندازه Swap (پیش‌فرض: ۸ گیگابایت در صورت فضای کافی)
#    --dir PATH           مسیر نصب (پیش‌فرض /opt/xagent)
#    --branch NAME        شاخه GitHub (پیش‌فرض main)
#    --repo URL           آدرس مخزن
#    --mirror URL         Docker registry mirror (مثلاً برای سرورهای داخل ایران)
#    --pip-index URL      آینه PyPI برای ساخت ایمیج
#    --npm-registry URL   آینه npm برای ساخت ایمیج
#    --timezone TZ        منطقه زمانی پیش‌فرض (Asia/Tehran)
#    --no-firewall        عدم تنظیم UFW
# ════════════════════════════════════════════════════════════════════
set -Eeuo pipefail

REPO_URL="https://github.com/omerae7/Xagent.git"
BRANCH="main"
XAGENT_HOME="/opt/xagent"
DOMAIN=""
EMAIL=""
PROFILE=""
DESKTOP="auto"
SWAP_GB=""
MIRROR=""
PIP_INDEX=""
NPM_REGISTRY=""
TIMEZONE="Asia/Tehran"
FIREWALL=1

c_blue='\033[1;34m'; c_green='\033[1;32m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_off='\033[0m'
step() { echo -e "\n${c_blue}▶ $*${c_off}"; }
ok()   { echo -e "${c_green}✓ $*${c_off}"; }
warn() { echo -e "${c_yellow}⚠ $*${c_off}"; }
die()  { echo -e "${c_red}✗ $*${c_off}" >&2; exit 1; }
trap 'die "نصب در خط $LINENO با خطا متوقف شد. برای جزئیات خروجی بالا را ببینید."' ERR

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --no-desktop) DESKTOP="off"; shift ;;
    --with-desktop) DESKTOP="on"; shift ;;
    --swap) SWAP_GB="$2"; shift 2 ;;
    --dir) XAGENT_HOME="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --mirror) MIRROR="$2"; shift 2 ;;
    --pip-index) PIP_INDEX="$2"; shift 2 ;;
    --npm-registry) NPM_REGISTRY="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --no-firewall) FIREWALL=0; shift ;;
    -h|--help) sed -n '2,32p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "گزینه ناشناخته: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "این اسکریپت باید با sudo یا کاربر root اجرا شود."
export DEBIAN_FRONTEND=noninteractive

echo -e "${c_blue}"
cat <<'BANNER'
   __  __      _                    _
   \ \/ /     /_\   __ _  ___ _ __ | |_
    >  <     //_\\ / _` |/ _ \ '_ \| __|
   /_/\_\   /  _  \ (_| |  __/ | | | |_
            \_/ \_/\__, |\___|_| |_|\__|
                   |___/   کارمند دیجیتال ۲۴ساعته
BANNER
echo -e "${c_off}"

# ───────────── ۱. بررسی سیستم و سخت‌افزار ─────────────
step "بررسی سیستم‌عامل و سخت‌افزار"
. /etc/os-release || die "سیستم‌عامل قابل تشخیص نیست"
case "$ID:$VERSION_ID" in
  ubuntu:24.04|ubuntu:22.04|debian:12) ok "سیستم‌عامل: $PRETTY_NAME" ;;
  *) warn "سیستم‌عامل $PRETTY_NAME رسماً پشتیبانی نمی‌شود؛ نصب ادامه می‌یابد." ;;
esac
ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64) ;; *) die "معماری $ARCH پشتیبانی نمی‌شود (x86_64 یا aarch64 لازم است)";; esac
CPUS="$(nproc)"
RAM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
mkdir -p "$XAGENT_HOME"
DISK_FREE_GB="$(df -BG --output=avail "$XAGENT_HOME" | tail -1 | tr -dc '0-9')"
ok "پردازنده: ${CPUS} هسته | حافظه: ${RAM_MB}MB | فضای آزاد: ${DISK_FREE_GB}GB | معماری: $ARCH"
[ "$DISK_FREE_GB" -ge 10 ] || die "حداقل ۱۰ گیگابایت فضای آزاد لازم است."

if [ -z "$PROFILE" ]; then
  if [ "$RAM_MB" -lt 3800 ]; then PROFILE="lite"
  elif [ "$RAM_MB" -ge 15000 ] && [ "$CPUS" -ge 6 ]; then PROFILE="performance"
  else PROFILE="standard"; fi
fi
if [ "$DESKTOP" = "auto" ]; then
  if [ "$PROFILE" = "lite" ]; then DESKTOP="off"; else DESKTOP="on"; fi
fi
ok "پروفایل اجرا: $PROFILE | دسکتاپ ایجنت: $DESKTOP"

case "$PROFILE" in
  lite)
    PG_SB=128MB; PG_EC=512MB; PG_WM=4MB; PG_MM=64MB; PG_MC=40; PG_ML=512m
    R_MAX=96mb; R_ML=160m; API_ML=512m; WK_ML=768m; DK_ML=1536m; GEOM=1280x720x24; API_W=1 ;;
  performance)
    PG_SB=2GB; PG_EC=6GB; PG_WM=32MB; PG_MM=512MB; PG_MC=200; PG_ML=4g
    R_MAX=512mb; R_ML=768m; API_ML=1536m; WK_ML=3g; DK_ML=4g; GEOM=1920x1080x24; API_W=4 ;;
  *)
    PROFILE=standard
    PG_SB=512MB; PG_EC=2GB; PG_WM=16MB; PG_MM=128MB; PG_MC=100; PG_ML=1536m
    R_MAX=256mb; R_ML=384m; API_ML=768m; WK_ML=1536m; DK_ML=2560m; GEOM=1600x900x24
    API_W=$(( CPUS >= 4 ? 2 : 1 )) ;;
esac

# ───────────── ۲. Swap ─────────────
step "تنظیم حافظه مجازی (Swap)"
SWAP_MB="$(awk '/SwapTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
if [ -z "$SWAP_GB" ]; then
  if [ "$DISK_FREE_GB" -ge 40 ]; then SWAP_GB=8; elif [ "$DISK_FREE_GB" -ge 25 ]; then SWAP_GB=4; else SWAP_GB=2; fi
fi
if [ "$SWAP_MB" -lt $(( SWAP_GB * 1024 / 2 )) ]; then
  if [ ! -f /swapfile ]; then
    fallocate -l "${SWAP_GB}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$(( SWAP_GB * 1024 )) status=none
    chmod 600 /swapfile && mkswap /swapfile >/dev/null
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  swapon /swapfile 2>/dev/null || true
  ok "Swap ${SWAP_GB}GB فعال شد"
else
  ok "Swap موجود: ${SWAP_MB}MB"
fi
sysctl -q -w vm.swappiness=10 vm.vfs_cache_pressure=50 >/dev/null || true
cat > /etc/sysctl.d/90-xagent.conf <<'EOF'
vm.swappiness=10
vm.vfs_cache_pressure=50
vm.overcommit_memory=1
EOF

# ───────────── ۳. پیش‌نیازها و Docker ─────────────
step "نصب پیش‌نیازها"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git jq openssl ufw iproute2 >/dev/null
ok "بسته‌های پایه نصب شدند"

if [ -n "$MIRROR" ]; then
  mkdir -p /etc/docker
  printf '{\n  "registry-mirrors": ["%s"],\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}\n' "$MIRROR" > /etc/docker/daemon.json
  ok "Registry mirror تنظیم شد: $MIRROR"
fi

if ! command -v docker >/dev/null 2>&1; then
  step "نصب Docker"
  if curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh >/dev/null 2>&1; then
    ok "Docker (مخزن رسمی) نصب شد"
  else
    warn "مخزن رسمی Docker در دسترس نبود؛ نصب از مخزن سیستم‌عامل…"
    apt-get install -y -qq docker.io >/dev/null
    apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
  fi
fi
systemctl enable --now docker >/dev/null 2>&1 || true
[ -n "$MIRROR" ] && systemctl restart docker
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 در دسترس نیست."
ok "$(docker --version | cut -d, -f1) | $(docker compose version --short 2>/dev/null || echo compose)"

# ───────────── ۴. فایروال ─────────────
if [ "$FIREWALL" = "1" ]; then
  step "تنظیم فایروال (UFW)"
  SSH_PORTS="$(ss -tlnpH 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u | tr '\n' ' ')"
  for p in 22 $SSH_PORTS; do ufw allow "$p/tcp" >/dev/null; done
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null
  ufw --force enable >/dev/null
  ok "فقط SSH (${SSH_PORTS:-22})، 80 و 443 باز هستند؛ دیتابیس، Redis و دسکتاپ هرگز منتشر نمی‌شوند."
fi

# ───────────── ۵. دریافت کد ─────────────
step "دریافت کد از GitHub"
APP="$XAGENT_HOME/app"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch --depth 50 origin "$BRANCH" && git -C "$APP" reset --hard "origin/$BRANCH"
else
  git clone --depth 50 --branch "$BRANCH" "$REPO_URL" "$APP"
fi
COMMIT="$(git -C "$APP" rev-parse HEAD)"
ok "نسخه: ${COMMIT:0:10}"

# ───────────── ۶. پیکربندی ─────────────
step "ساخت پیکربندی امن (.env)"
mkdir -p "$XAGENT_HOME"/{data/control,pgdata,redis,desktop-home,runner-home,caddy/data,caddy/config,backups}
chown -R 1001:1001 "$XAGENT_HOME/data"
chown -R 1000:1000 "$XAGENT_HOME/desktop-home" "$XAGENT_HOME/runner-home"
ENV_FILE="$APP/.env"
gen() { openssl rand -hex "${1:-32}"; }
getv() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }
SECRET="$(getv XAGENT_SECRET_KEY)"; SECRET="${SECRET:-$(gen 32)}"
PGPASS="$(getv POSTGRES_PASSWORD)"; PGPASS="${PGPASS:-$(gen 24)}"
DTOKEN="$(getv DESKTOP_TOKEN)"; DTOKEN="${DTOKEN:-$(gen 24)}"
RTOKEN="$(getv RUNNER_TOKEN)"; RTOKEN="${RTOKEN:-$(gen 24)}"

if [ -n "$DOMAIN" ]; then
  SITE="$DOMAIN"; TLS="${EMAIL:-admin@$DOMAIN}"; PUBLIC_URL="https://$DOMAIN"
else
  IP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  SITE="https://$IP"; TLS="internal"; PUBLIC_URL="https://$IP"
fi
if [ "$DESKTOP" = "on" ]; then PROFILES="desktop"; SANDBOX="desktop"; else PROFILES=""; SANDBOX="runner"; fi

umask 077
cat > "$ENV_FILE" <<EOF
# ── X Agent — پیکربندی (ساخته‌شده توسط install.sh در $(date -u +%F)) ──
# این فایل حاوی اسرار است؛ آن را منتشر نکنید.
XAGENT_ENV=production
XAGENT_PROFILE=$PROFILE
XAGENT_HOME=$XAGENT_HOME
XAGENT_SECRET_KEY=$SECRET
POSTGRES_PASSWORD=$PGPASS
DESKTOP_TOKEN=$DTOKEN
RUNNER_TOKEN=$RTOKEN
XAGENT_PUBLIC_URL=$PUBLIC_URL
XAGENT_SITE_ADDRESS=$SITE
XAGENT_TLS=$TLS
# Empty uses the host in XAGENT_SITE_ADDRESS (also supports pre-existing .env files).
XAGENT_DEFAULT_SNI=
XAGENT_DEFAULT_TIMEZONE=$TIMEZONE
XAGENT_COOKIE_SECURE=true
XAGENT_REPO_URL=$REPO_URL
XAGENT_REPO_BRANCH=$BRANCH
XAGENT_IMAGE_TAG=${COMMIT:0:12}
COMPOSE_PROFILES=$PROFILES
XAGENT_SANDBOX=$SANDBOX
XAGENT_API_WORKERS=$API_W
PG_SHARED_BUFFERS=$PG_SB
PG_EFFECTIVE_CACHE=$PG_EC
PG_WORK_MEM=$PG_WM
PG_MAINTENANCE_MEM=$PG_MM
PG_MAX_CONNECTIONS=$PG_MC
PG_MEM_LIMIT=$PG_ML
REDIS_MAXMEMORY=$R_MAX
REDIS_MEM_LIMIT=$R_ML
API_MEM_LIMIT=$API_ML
WORKER_MEM_LIMIT=$WK_ML
DESKTOP_MEM_LIMIT=$DK_ML
RUNNER_MEM_LIMIT=768m
DESKTOP_GEOMETRY=$GEOM
PIP_INDEX_URL=${PIP_INDEX:-https://pypi.org/simple}
NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmjs.org}
EOF
umask 022
chmod 600 "$ENV_FILE"
ok "پیکربندی در $ENV_FILE ذخیره شد (دسترسی فقط root)"

# ───────────── ۷. ساخت و اجرا ─────────────
step "ساخت ایمیج‌ها و اجرای سرویس‌ها (ممکن است چند دقیقه طول بکشد)"
cd "$APP"
docker compose build
docker compose up -d --remove-orphans

step "انتظار برای آماده شدن سیستم"
for i in $(seq 1 90); do
  if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" >/dev/null 2>&1; then
    ok "API آماده است"; break
  fi
  [ "$i" -eq 90 ] && die "سرویس API آماده نشد. لاگ: docker compose -f $APP/compose.yaml logs api"
  sleep 4
done
printf '{"version":"%s","commit":"%s","installed_at":"%s"}\n' "$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)" "$COMMIT" "$(date -u +%FT%TZ)" > "$XAGENT_HOME/data/control/version.json"
chown 1001:1001 "$XAGENT_HOME/data/control/version.json"

# ───────────── ۸. به‌روزرسان و ابزار مدیریت ─────────────
step "نصب سرویس به‌روزرسانی امن و دستور xagent"
sed "s#/opt/xagent#$XAGENT_HOME#g" infra/updater/xagent-updater.path > /etc/systemd/system/xagent-updater.path
sed "s#/opt/xagent#$XAGENT_HOME#g" infra/updater/xagent-updater.service > /etc/systemd/system/xagent-updater.service
systemctl daemon-reload
systemctl enable --now xagent-updater.path >/dev/null
mkdir -p /usr/local/lib/xagent
install -m 0755 infra/scripts/xagentctl /usr/local/lib/xagent/xagentctl
install -m 0755 infra/updater/xagent-update.sh /usr/local/lib/xagent/xagent-update
ln -sf /usr/local/lib/xagent/xagentctl /usr/local/bin/xagent
printf 'XAGENT_HOME=%s\n' "$XAGENT_HOME" > /etc/xagent.conf
bash infra/hostd/install-hostd.sh "$XAGENT_HOME" /usr/local/lib/xagent "$APP/infra/hostd" || warn "نصب کارگزار فرمان میزبان ناموفق بود"
ok "به‌روزرسانی از پنل (تنظیمات ← سیستم) یا با دستور «xagent update»"

PASS_FILE="$XAGENT_HOME/data/initial_password.txt"
PASS="$( [ -f "$PASS_FILE" ] && awk '/password:/ {print $2}' "$PASS_FILE" || echo '(قبلاً تنظیم شده است)')"

echo
echo -e "${c_green}════════════════════════════════════════════════════════${c_off}"
echo -e "${c_green}  ✅ X Agent با موفقیت نصب شد${c_off}"
echo -e "${c_green}════════════════════════════════════════════════════════${c_off}"
echo -e "  نشانی پنل:      ${c_blue}$PUBLIC_URL${c_off}"
echo -e "  نام کاربری:     admin"
echo -e "  رمز اولیه:      ${c_yellow}$PASS${c_off}"
echo -e "  پروفایل:        $PROFILE  (دسکتاپ: $DESKTOP)"
[ -z "$DOMAIN" ] && echo -e "  ${c_yellow}نکته: بدون دامنه، گواهی HTTPS داخلی است و مرورگر هشدار می‌دهد؛ برای گواهی معتبر با --domain نصب کنید.${c_off}"
echo -e "  پس از ورود، رمز را از «تنظیمات ← امنیت» تغییر دهید و از «تنظیمات ← هوش مصنوعی» مدل اضافه کنید."
echo -e "  مدیریت: xagent status | logs | backup | update | password | desktop on|off"
echo
