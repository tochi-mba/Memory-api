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
from memory_api.core.consolidator import start_consolidator, stop_consolidator
from memory_api.core.container import build_container
from memory_api.core.sweeper import start_sweeper, stop_sweeper

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from httpx import AsyncBaseTransport


def create_app(
    settings: Settings | None = None,
    *,
    transport: AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        settings: configuration; loaded from the environment when omitted.
        transport: optional httpx transport for tests (fake keyring).
    """
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        container = build_container(resolved, transport=transport)
        app.state.container = container
        # Erasure is a background job, not a request. See `core.sweeper` for why running it
        # at all is the difference between the route descriptions being true and not.
        sweeper = start_sweeper(
            container.store,
            grace_seconds=resolved.forget_grace_seconds,
            interval_seconds=resolved.sweep_interval_seconds,
        )
        consolidator = start_consolidator(
            container.store,
            idle_seconds=resolved.consolidate_idle_seconds,
            interval_seconds=resolved.consolidate_interval_seconds,
        )
        try:
            yield
        finally:
            await stop_consolidator(consolidator)
            await stop_sweeper(sweeper)
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
