"""FastAPI app factory + lifespan."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings
from .routes import attachments as attachments_routes
from .routes import interactions as interactions_routes
from .routes import kb as kb_routes
from .routes import messages as messages_routes
from .routes import outputs as outputs_routes
from .routes import sessions as sessions_routes
from .state import AppState


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # AgentRunner sets CLAUDE_CONFIG_DIR on the SDK subprocess so JSONL
        # transcripts land under settings.sessions_dir. The session-history
        # reader (`claude_agent_sdk.get_session_messages`) runs in THIS
        # parent process and reads CLAUDE_CONFIG_DIR from `os.environ`, so
        # we mirror the same value while the app is running and restore on
        # exit -- otherwise concurrent test fixtures with different
        # tmp_paths would leak the last-set value across the process.
        prev_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(settings.sessions_dir)
        state = AppState(settings)
        await state.registry.load()
        await state.kb.load()
        await state.outputs.load()
        app.state.app_state = state
        try:
            yield
        finally:
            await state.shutdown()
            if prev_config_dir is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prev_config_dir

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
    app.include_router(attachments_routes.router)
    app.include_router(outputs_routes.router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"ok": True}

    return app
