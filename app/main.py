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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/healthz":
        return JSONResponse({"ok": True})
    if path not in PUBLIC_PATHS and not path.startswith("/static"):
        user = read_session(request.cookies.get(SESSION_COOKIE))
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            login_url = f"/login?next={path}"
            return RedirectResponse(login_url, status_code=303)
        request.state.user = user
    return await call_next(request)
