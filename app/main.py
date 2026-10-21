"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, scheduler
from .config import settings
from .security import SESSION_COOKIE, read_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sheets2kdocs")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.db_path)
    scheduler.start(settings.timezone)
    try:
        from . import ext_service

        ext_service.ensure_admin()
    except Exception:
        logger.exception("ext admin bootstrap failed")
    logger.info("sheets2kdocs started (data dir: %s)", settings.data_dir.resolve())
    yield
    scheduler.shutdown()
    logger.info("sheets2kdocs stopped")


app = FastAPI(title="sheets2kdocs", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR.parent / "static")), name="static")

from .api_routes import router as api_router  # noqa: E402
from .web_routes import router as web_router  # noqa: E402

app.include_router(web_router)
app.include_router(api_router, prefix="/api")

PUBLIC_PATHS = {"/login", "/healthz"}
EXTENSION_PUSH_PATH = "/api/extension/push"
EXTENSION_PUBLIC_PATHS = {"/api/extension/auth/login", "/api/airscript"}
EXTENSION_AUTH_PREFIXES = ("/api/extension/auth/", "/api/extension/presets", "/api/extension/admin/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/healthz":
        return JSONResponse({"ok": True})
    if path == EXTENSION_PUSH_PATH:
        # browser-extension channel: X-Ext-Token instead of session cookie
        import hmac as _hmac

        from .kdocs_cli import get_setting

        try:
            expected = get_setting("ext_push_token") or ""
        except Exception:
            expected = ""
        supplied = request.headers.get("x-ext-token", "")
        if expected and supplied and _hmac.compare_digest(supplied, expected):
            request.state.user = "extension"
            return await call_next(request)
        return JSONResponse({"error": "invalid extension token"}, status_code=401)
    if path in EXTENSION_PUBLIC_PATHS:
        return await call_next(request)
    if path.startswith(EXTENSION_AUTH_PREFIXES):
        # extension login channel: X-Auth-Token
        from . import ext_service

        user = ext_service.auth(request.headers.get("x-auth-token", ""))
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        request.state.ext_user = user
        return await call_next(request)
    if path not in PUBLIC_PATHS and not path.startswith("/static"):
        user = read_session(request.cookies.get(SESSION_COOKIE))
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            login_url = f"/login?next={path}"
            return RedirectResponse(login_url, status_code=303)
        request.state.user = user
    return await call_next(request)
