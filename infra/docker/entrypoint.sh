#!/usr/bin/env bash
# نقطه ورود کانتینرهای Backend: api | worker | migrate | cli
set -euo pipefail
role="${1:-api}"
shift || true

wait_db() {
  for i in $(seq 1 60); do
    if python - <<'PY' 2>/dev/null; then return 0; fi
import asyncio, os, asyncpg
url = os.environ["XAGENT_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
async def main():
    c = await asyncpg.connect(url, timeout=3); await c.close()
asyncio.run(main())
PY
    echo "⏳ منتظر پایگاه داده… ($i)"; sleep 2
  done
  echo "❌ پایگاه داده در دسترس نیست"; exit 1
}

case "$role" in
  api)
    wait_db
    (cd /app/services/api && alembic upgrade head)
    workers="${XAGENT_API_WORKERS:-1}"
    exec uvicorn xagent_api.main:app --host 0.0.0.0 --port 8000 --workers "$workers" \
      --proxy-headers --forwarded-allow-ips="*" --timeout-keep-alive 75 "$@"
    ;;
  worker)
    wait_db
    exec python -m xagent_worker "$@"
    ;;
  migrate)
    wait_db
    cd /app/services/api && exec alembic upgrade head
    ;;
  cli)
    exec python -m xagent_api.cli "$@"
    ;;
  *)
    exec "$role" "$@"
    ;;
esac
