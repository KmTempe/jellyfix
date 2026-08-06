from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import JellyfinClient
from .config import Settings, load_settings
from .database import Database
from .notifications import outbox_worker
from .repositories import TicketRepository
from .routes import router


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_path)
    db.init()
    ticket_repo = TicketRepository(settings)
    jellyfin_client = JellyfinClient(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.outbox_task = asyncio.create_task(outbox_worker(app.state.db, settings))
        try:
            yield
        finally:
            task = app.state.outbox_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        root_path=settings.root_path,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.ticket_repo = ticket_repo
    app.state.jellyfin_client = jellyfin_client
    app.state.outbox_task = None

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    @app.middleware("http")
    async def request_security(request: Request, call_next: Callable):
        if request.method not in SAFE_METHODS and request.headers.get("origin") != settings.public_origin:
            return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > settings.max_body_bytes:
            return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.include_router(router)
    return app
