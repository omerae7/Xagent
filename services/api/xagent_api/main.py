"""نقطه ورود API پنل X Agent."""

from __future__ import annotations

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
from .routers import auth, chat, control, integrations, live, memory, settings, work

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_scrubber()
log = logging.getLogger("xagent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap()
    log.info("X Agent API آماده است (پروفایل: %s)", get_settings().profile)
    yield
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


for r in (auth, chat, live, work, memory, settings, integrations, control):
    app.include_router(r.router)


@app.get("/api/health")
async def health():
    ok_db = ok_redis = False
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("select 1"))
        ok_db = True
    except Exception:  # noqa: BLE001
        pass
    try:
        ok_redis = bool(await bus.get_redis().ping())
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"ok": ok_db and ok_redis, "db": ok_db, "redis": ok_redis},
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
