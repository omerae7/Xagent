#!/usr/bin/env bash
# Chromium با پروفایل پایدار (لاگین‌ها پس از ری‌استارت حفظ می‌شوند) و پورت CDP محلی برای Playwright
PROFILE="$HOME/.config/xagent-chromium"
mkdir -p "$PROFILE"
chmod 700 "$HOME/.config" "$PROFILE" 2>/dev/null || true
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"
exec /usr/bin/chromium \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port=9223 \
  --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check \
  --disable-dev-shm-usage --no-sandbox --test-type \
  --lang=fa --start-maximized \
  --password-store=basic \
  about:blank
