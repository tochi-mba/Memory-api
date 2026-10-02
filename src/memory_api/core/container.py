"""Process-scoped object graph."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keyring_client import JwksClient, SystemClock

from memory_api.auth.services import ServiceAuthenticator
from memory_api.auth.verifier import TokenVerifier
from memory_api.core.preferences import build_preference_source
from memory_api.store.worker import StoreWorker

if TYPE_CHECKING:
    import httpx

    from memory_api.core.config import Settings
    from memory_api.core.preferences import PreferenceSource


@dataclass(slots=True)
class Container:
    """What every request shares for the life of the process."""

    settings: Settings
    jwks: JwksClient
    verifier: TokenVerifier
    services: ServiceAuthenticator
    store: StoreWorker
    preferences: PreferenceSource
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    async def aclose(self) -> None:
        """Release every long-lived resource, even if an earlier one objects.

        The store's thread is shut down whatever the HTTP clients do on the way out. A
        leaked thread holds an open SQLite connection, and on a file-backed database that
        means a WAL that is never checkpointed.
        """
        try:
            try:
                await self.jwks.aclose()
            finally:
                await self.preferences.aclose()
        finally:
            await self.store.aclose()


def build_container(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    preferences: PreferenceSource | None = None,
) -> Container:
    """Assemble JWKS client, verifier, preference source and store worker. No network I/O yet.

    The worker opens its connection on its own thread as soon as it is constructed, so a
    database that cannot be opened surfaces on the first call rather than at import time.

    Args:
        settings: the configuration.
        transport: an httpx transport for keyring, substituted by tests.
        preferences: substituted by tests, which read people's settings from a fake rather
            than a settings-api; built from ``settings`` when omitted.
    """
    clock = SystemClock()
    jwks = JwksClient(
        url=settings.keyring_jwks_url,
        clock=clock,
        cache_seconds=settings.jwks_cache_seconds,
        min_refetch_seconds=settings.jwks_min_refetch_seconds,
        timeout_seconds=settings.keyring_timeout_seconds,
        transport=transport,
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
        services=ServiceAuthenticator(settings.service_tokens),
        store=StoreWorker(settings.database_path),
        preferences=preferences if preferences is not None else build_preference_source(settings),
    )
