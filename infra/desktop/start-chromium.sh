#!/usr/bin/env bash
# Browser/process, persistent identity and Run lifetime are independent.
set -euo pipefail
choice="${XAGENT_BROWSER:-auto}"
legacy="$HOME/.config/xagent-chromium"
chrome="$HOME/.config/xagent-chrome"
case "$choice" in
  auto)
    # Existing installations retain their logged-in Chromium profile. An explicit
    # opt-in starts a separate Chrome identity; credentials are never copied.
    if [ -f "$legacy/Local State" ] && [ ! -f "$chrome/Local State" ]; then choice=chromium
    elif command -v google-chrome-stable >/dev/null; then choice=chrome
    else choice=chromium; fi ;;
  chrome|chromium) ;;
  *) echo 'XAGENT_BROWSER must be auto, chrome or chromium' >&2; exit 2 ;;
esac
if [ "$choice" = chrome ]; then BROWSER=/usr/bin/google-chrome-stable; PROFILE="$chrome"
else BROWSER=/usr/bin/chromium; PROFILE="$legacy"; fi
test -x "$BROWSER" || { echo "Selected browser unavailable: $choice" >&2; exit 1; }
mkdir -p "$PROFILE"
chmod 700 "$HOME/.config" "$PROFILE" 2>/dev/null || true
# Let Chrome validate SingletonLock itself. Removing a live lock can corrupt profiles.
"$BROWSER" --version
exec "$BROWSER" \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port=9223 \
  --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check \
  --disable-dev-shm-usage --no-sandbox --test-type \
  --lang=fa --start-maximized \
  --password-store=basic \
  --restore-last-session
