"""Application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from memory_api import __version__
from memory_api.api.errors import register_exception_handlers
from memory_api.api.middleware import RequestContextMiddleware
from memory_api.api.routers import ROUTERS
from memory_api.core.config import Settings, load_settings
from memory_api.core.container import build_container

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_app(
    settings: Settings | None = None,
    *,
    transport: object | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        settings: configuration; loaded from the environment when omitted.
        transport: optional httpx transport for tests (fake keyring).
    """
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(resolved, transport=transport)
        app.state.container = container
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="Memory API",
        description="LUCY-family memory: what is remembered about a person, and why.",
        version=__version__,
        lifespan=lifespan,
    )
    # Outermost, so the request id is bound before anything else runs and is still bound
    # when an unhandled exception is turned into the 500 that tells the caller to quote it.
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    return app
