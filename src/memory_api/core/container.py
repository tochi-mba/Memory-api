"""Process-scoped object graph."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keyring_client import JwksClient, SystemClock

from memory_api.auth.verifier import TokenVerifier
from memory_api.store.worker import StoreWorker

if TYPE_CHECKING:
    from memory_api.core.config import Settings


@dataclass(slots=True)
class Container:
    """What every request shares for the life of the process."""

    settings: Settings
    jwks: JwksClient
    verifier: TokenVerifier
    store: StoreWorker
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    async def aclose(self) -> None:
        """Release both long-lived resources, even if the first one objects.

        The store's thread is shut down whatever the JWKS client does on the way out. A
        leaked thread holds an open SQLite connection, and on a file-backed database that
        means a WAL that is never checkpointed.
        """
        try:
            await self.jwks.aclose()
        finally:
            await self.store.aclose()


def build_container(settings: Settings, *, transport: object | None = None) -> Container:
    """Assemble JWKS client, verifier and store worker. No network I/O yet.

    The worker opens its connection on its own thread as soon as it is constructed, so a
    database that cannot be opened surfaces on the first call rather than at import time.
    """
    clock = SystemClock()
    jwks = JwksClient(
        url=settings.keyring_jwks_url,
        clock=clock,
        cache_seconds=settings.jwks_cache_seconds,
        min_refetch_seconds=settings.jwks_min_refetch_seconds,
        timeout_seconds=settings.keyring_timeout_seconds,
        transport=transport,  # type: ignore[arg-type]
    )
    verifier = TokenVerifier(
        jwks=jwks,
        issuer=settings.keyring_issuer,
        audience=settings.audience,
        clock=clock,
    )
    return Container(
        settings=settings,
        jwks=jwks,
        verifier=verifier,
        store=StoreWorker(settings.database_path),
    )
