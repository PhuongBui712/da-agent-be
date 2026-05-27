"""FastAPI app factory + lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings
from .routes import interactions as interactions_routes
from .routes import kb as kb_routes
from .routes import messages as messages_routes
from .routes import sessions as sessions_routes
from .state import AppState


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = AppState(settings)
        await state.registry.load()
        await state.kb.load()
        app.state.app_state = state
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="DA-Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(sessions_routes.router)
    app.include_router(messages_routes.router)
    app.include_router(interactions_routes.router)
    app.include_router(kb_routes.router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"ok": True}

    return app
