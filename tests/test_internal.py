"""The two-credential internal surface.

A sibling service must present its own token *and* the person's memory-api token. The
account still comes from the person's token, so a service cannot name who it is asking
about. A stolen user token without the service credential cannot use this path; a service
token without a person cannot either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, mint

from conftest import ACCOUNT, AUDIENCE, OTHER_ACCOUNT, build_settings
from memory_api.api.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

SERVICE_TOKEN = "n" * 32
USER_TOKEN_HEADER = "X-Keyring-User-Token"


@pytest.fixture
async def internal(keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    settings = build_settings(service_tokens={"lucy-api": SERVICE_TOKEN})
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


def headers(account: str = ACCOUNT) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        USER_TOKEN_HEADER: mint(account_id=account, audience=AUDIENCE, issuer=ISSUER),
    }


async def test_a_service_can_search_for_the_person_whose_token_it_holds(
    internal: AsyncClient,
) -> None:
    created = await internal.post(
        "/v1/internal/memory",
        headers=headers(),
        json={"title": "Home city", "body": "Bristol"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["asserted_by"] == "lucy-api"
    found = await internal.get("/v1/internal/memory/search?q=city", headers=headers())
    assert found.status_code == 200
    assert [row["body"] for row in found.json()["data"]] == ["Bristol"]
    listed = await internal.get("/v1/internal/memory", headers=headers())
    assert listed.json()["data"][0]["id"] == created.json()["id"]
    topics = await internal.get("/v1/internal/memory/topics", headers=headers())
    assert topics.status_code == 200
    topic_id = topics.json()["data"][0]["id"]
    expanded = await internal.get(f"/v1/internal/memory/topics/{topic_id}", headers=headers())
    assert expanded.status_code == 200
    assert expanded.json()["memories"]
    blocks = await internal.get("/v1/internal/memory/blocks", headers=headers())
    assert blocks.status_code == 200
    assert blocks.json()["data"] == []


async def test_a_service_can_correct_confirm_and_forget(internal: AsyncClient) -> None:
    created = await internal.post(
        "/v1/internal/memory",
        headers=headers(),
        json={"title": "Tea", "body": "from a page", "trust": "untrusted"},
    )
    memory_id = created.json()["id"]
    confirmed = await internal.post(f"/v1/internal/memory/{memory_id}/confirm", headers=headers())
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_at"] is not None
    corrected = await internal.post(
        f"/v1/internal/memory/{memory_id}/correct",
        headers=headers(),
        json={"title": "Tea", "body": "Earl Grey"},
    )
    assert corrected.status_code == 201
    forgotten = await internal.post(
        f"/v1/internal/memory/{corrected.json()['id']}/forget", headers=headers()
    )
    assert forgotten.status_code == 200
    assert forgotten.json()["forgotten_at"] is not None


async def test_the_person_token_alone_cannot_use_the_internal_path(internal: AsyncClient) -> None:
    token = mint(account_id=ACCOUNT, audience=AUDIENCE, issuer=ISSUER)
    response = await internal.get(
        "/v1/internal/memory/search",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


async def test_a_service_token_without_a_person_is_refused(internal: AsyncClient) -> None:
    response = await internal.get(
        "/v1/internal/memory/search",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert response.status_code == 401


async def test_a_blank_person_token_is_refused(internal: AsyncClient) -> None:
    response = await internal.get(
        "/v1/internal/memory/search",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}", USER_TOKEN_HEADER: "   "},
    )
    assert response.status_code == 401


async def test_every_internal_refusal_is_identical(internal: AsyncClient) -> None:
    missing = await internal.get("/v1/internal/memory/search")
    stolen = await internal.get(
        "/v1/internal/memory/search",
        headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            USER_TOKEN_HEADER: mint(account_id=ACCOUNT, audience="settings", issuer=ISSUER),
        },
    )
    assert missing.status_code == stolen.status_code == 401
    assert missing.json()["detail"] == stolen.json()["detail"]


async def test_another_account_is_a_miss_not_a_refusal(internal: AsyncClient) -> None:
    created = await internal.post(
        "/v1/internal/memory",
        headers=headers(),
        json={"title": "Secret preference", "body": "Earl Grey"},
    )
    memory_id = created.json()["id"]
    stranger = await internal.get(
        f"/v1/internal/memory/{memory_id}",
        headers=headers(OTHER_ACCOUNT),
    )
    assert stranger.status_code == 404


async def test_an_unconfigured_service_is_refused(keyring: FakeKeyring) -> None:
    app = create_app(build_settings(), transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        response = await http.get("/v1/internal/memory/search", headers=headers())
    assert response.status_code == 401
