"""Shared fixtures.

Two kinds of test live in this suite and they need different handles on the same code. The
HTTP tests drive the real app through ``client``, which is the only way to check the things
that are properties of the *surface*: route order, who a token says you are, the shape of a
refusal. The store tests hold a :class:`SQLStore` directly, because behaviour like cursor
paging and the sweep is easier to pin one call at a time than through fifteen requests.

The store fixture is safe to use from the test's own thread even though the connection
enforces ``check_same_thread``: it is constructed here and used here. Only the worker needs
a thread of its own, and it has one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, JWKS_URL, FakeKeyring, mint

from memory_api.api.app import create_app
from memory_api.core.config import LogFormat, Settings
from memory_api.store.sql import SQLStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

AUDIENCE = "memory-api"
ACCOUNT = "acct_example"
OTHER_ACCOUNT = "acct_someone_else"


def build_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "audience": AUDIENCE,
        # Every test gets its own database, created and destroyed with its worker thread. A
        # shared file would make the suite order-dependent in the way that is hardest to
        # see: a test that passes alone and fails after another one wrote a memory.
        "database_path": ":memory:",
    }
    return Settings(**{**defaults, **overrides})


class FakeClock:
    """Time the test moves by hand.

    Temporal reasoning is most of what this service does -- what was true then, what is
    true now, what a correction replaced -- and none of it is testable against a clock that
    advances on its own. Sub-second real time also makes decay and recency ties unstable.
    """

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> Iterator[SQLStore]:
    handle = SQLStore(":memory:", clock)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
async def client(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


def bearer(account_id: str = ACCOUNT, audience: str = AUDIENCE) -> dict[str, str]:
    token = mint(account_id=account_id, audience=audience, issuer=ISSUER)
    return {"Authorization": f"Bearer {token}"}
