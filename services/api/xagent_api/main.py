"""نقطه ورود API پنل X Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.db import dispose_engine, get_engine
from xagent_core.redact import install_log_scrubber, scrub

from .bootstrap import bootstrap
from .routers import (
    auth,
    chat,
    control,
    external_agents,
    host,
    inbox,
    insights,
    integrations,
    live,
    memory,
    settings,
    work,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_scrubber()
log = logging.getLogger("xagent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from xagent_core.config import assert_production_secrets

    assert_production_secrets()  # کلید/توکن پیش‌فرض در production: اجرا نمی‌شود
    await bootstrap()
    log.info("X Agent API آماده است (پروفایل: %s)", get_settings().profile)
    from . import hostops

    reconcile = asyncio.create_task(hostops.reconcile_loop())
    yield
    reconcile.cancel()
    await bus.close_redis()
    await dispose_engine()


app = FastAPI(title="X Agent API", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException):
    """هیچ پیام خطایی بدون پاک‌سازی اسرار از API خارج نمی‌شود."""
    detail = scrub(exc.detail) if isinstance(exc.detail, str) else exc.detail
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errors = [f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg')}" for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "داده نامعتبر: " + "؛ ".join(errors)})


for r in (auth, chat, live, work, memory, settings, integrations, control, host, inbox, insights, external_agents):
    app.include_router(r.router)


HEALTH_TIMEOUT = 3.0


async def _db_ping() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("select 1"))


@app.get("/api/health")
async def health():
    """زنده‌بودن API + وابستگی‌ها با سقف زمان (درخواست سلامت هرگز پشت Pool پرشده گیر نمی‌کند).

    ok همان معنای قبلی (DB و Redis) را دارد؛ worker آمادگی عملکردی حلقه‌های Worker را جدا گزارش می‌کند.
    """
    ok_db = ok_redis = False
    try:
        await asyncio.wait_for(_db_ping(), timeout=HEALTH_TIMEOUT)
        ok_db = True
    except Exception:  # noqa: BLE001
        pass
    worker: dict = {"ready": None, "detail": "unknown"}
    try:
        r = bus.get_redis()
        ok_redis = bool(await asyncio.wait_for(r.ping(), timeout=HEALTH_TIMEOUT))
        raw = await asyncio.wait_for(r.get("xagent:worker:readiness"), timeout=HEALTH_TIMEOUT)
        if raw:
            data = json.loads(raw)
            worker = {"ready": bool(data.get("ready")), "degraded": data.get("degraded", []),
                      "intake": data.get("intake"), "at": data.get("at")}
        else:
            worker = {"ready": False, "detail": "no_recent_worker_report"}
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"ok": ok_db and ok_redis, "db": ok_db, "redis": ok_redis, "worker": worker},
                        status_code=200 if ok_db and ok_redis else 503)


# حالت سبک: سرو مستقیم پنل ساخته‌شده از API (بدون Caddy جداگانه)
_web = Path(os.environ.get("XAGENT_WEB_DIST", "")) if os.environ.get("XAGENT_WEB_DIST") else None
if _web and _web.is_dir():

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "یافت نشد"}, status_code=404)
        f = (_web / path).resolve()
        if path and f.is_file() and _web.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(_web / "index.html")
