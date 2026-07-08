"""knowts application entry point.

Wires configuration, the SQLite connection + migrations, the login rate
limiter, static files, templates, and the Phase 1 routers together. Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from . import db as db_mod
from . import prompts as prompts_mod
from .bootstrap import bootstrap_admin
from .config import get_settings
from .jobs import PipelineWorker
from .rate_limit import LoginRateLimiter
from .routers import admin_users, auth_routes, home, meetings, profile
from .templating import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("knowts")

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    conn = db_mod.connect(settings.db_path)
    version = db_mod.run_migrations(conn)
    log.info("Database ready at %s (schema v%d)", settings.db_path, version)

    bootstrap_admin(conn, settings)
    prompts_mod.seed_starter_prompts(conn)

    app.state.settings = settings
    app.state.db = conn
    app.state.login_limiter = LoginRateLimiter(
        max_attempts=settings.login_max_attempts,
        lockout_seconds=settings.login_lockout_seconds,
    )

    worker = PipelineWorker(settings)
    worker.start()
    await worker.recover_and_resume()
    app.state.worker = worker

    try:
        yield
    finally:
        await worker.stop()
        conn.close()


app = FastAPI(title="knowts", version=__version__, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(auth_routes.router)
app.include_router(home.router)
app.include_router(meetings.router)
app.include_router(profile.router)
app.include_router(admin_users.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "version": __version__}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Send browsers to the login page on 401; render friendly error pages
    otherwise. Non-GET requests just get the plain status."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and request.method == "GET":
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        return RedirectResponse(
            f"/login?next={next_url}", status_code=status.HTTP_303_SEE_OTHER
        )
    if request.method == "GET" and exc.status_code in (403, 404):
        return render(
            request,
            "error.html",
            status_code=exc.status_code,
            code=exc.status_code,
            detail=exc.detail,
        )
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("Bad request.", status_code=status.HTTP_400_BAD_REQUEST)
