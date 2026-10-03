"""A person's importance floor, over HTTP.

What these pin is where the floor bites: a create and a batch ADD, on both surfaces, and
nowhere else. A correction, a read, a forget and a batch that adds nothing go through
whatever settings-api is doing, because the floor has no say in them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, mint
from settings_client.testing import FakeSettingsClient

from conftest import ACCOUNT, AUDIENCE, bearer, build_settings
from memory_api.api.app import create_app
from memory_api.api.dependencies import PreferencesDep, ServicePreferencesDep
from memory_api.core.preferences import FLOOR, NAMESPACE, SettingsApiPreferences

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

SERVICE_TOKEN = "f" * 32
PROBLEMS = "https://memory-api.invalid/problems/"


@pytest.fixture
def settings_api() -> FakeSettingsClient:
    return FakeSettingsClient({NAMESPACE: {FLOOR: 4}})


@pytest.fixture
async def http(
    keyring: FakeKeyring, settings_api: FakeSettingsClient
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        build_settings(service_tokens={"lucy-api": SERVICE_TOKEN}),
        transport=keyring.transport(),
        preferences=SettingsApiPreferences(client=settings_api),
    )
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


def internal_headers(user_token: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Keyring-User-Token": user_token
        or mint(account_id=ACCOUNT, audience=AUDIENCE, issuer=ISSUER),
    }


def forged() -> str:
    """A token for this account and this service, signed by a key keyring never held."""
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return mint(account_id=ACCOUNT, audience=AUDIENCE, issuer=ISSUER, key=stranger)


async def stored(http: AsyncClient) -> list[str]:
    response = await http.get("/v1/memory", headers=bearer())
    assert response.status_code == 200, response.text
    return [row["title"] for row in response.json()["data"]]


class TestCreating:
    async def test_a_memory_below_the_floor_is_refused_and_not_stored(
        self, http: AsyncClient
    ) -> None:
        """The bug, named: a person who chose to have only what matters remembered still
        had every passing remark written down, because no write read their floor."""
        response = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Had toast", "importance": 3}
        )

        assert response.status_code == 422
        assert response.json()["type"] == PROBLEMS + "below-importance-floor"
        assert "toast" not in response.text
        assert await stored(http) == []

    async def test_a_memory_at_the_floor_is_stored(self, http: AsyncClient) -> None:
        response = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Allergic to nuts", "importance": 4}
        )

        assert response.status_code == 201, response.text

    async def test_a_sibling_write_that_clears_the_floor_is_stored(self, http: AsyncClient) -> None:
        unnamed = await http.post(
            "/v1/internal/memory", headers=internal_headers(), json={"title": "Had toast"}
        )
        kept = await http.post(
            "/v1/internal/memory",
            headers=internal_headers(),
            json={"title": "Allergic to nuts", "importance": 9},
        )

        # Five is what a write that names no importance carries, and four is the floor.
        assert unnamed.status_code == 201
        assert kept.status_code == 201
        assert await stored(http) == ["Had toast", "Allergic to nuts"]

    @pytest.mark.parametrize("settings_api", [FakeSettingsClient({NAMESPACE: {FLOOR: 6}})])
    async def test_a_sibling_write_below_the_floor_is_refused(self, http: AsyncClient) -> None:
        """The bug, named: the internal surface is how the assistant writes, so a floor only
        the person-facing route honoured would be a floor the assistant ignored."""
        response = await http.post(
            "/v1/internal/memory", headers=internal_headers(), json={"title": "Had toast"}
        )

        assert response.status_code == 422
        assert response.json()["type"] == PROBLEMS + "below-importance-floor"

    async def test_settings_api_is_never_shown_a_token_this_service_refused(
        self, http: AsyncClient, settings_api: FakeSettingsClient
    ) -> None:
        """The bug, named: asking settings-api before verifying would send a forged token on
        to another service in this one's name."""
        response = await http.post(
            "/v1/memory",
            headers={"Authorization": "Bearer not-a-token"},
            json={"title": "x"},
        )

        assert response.status_code == 401
        assert settings_api.resolves == 0


class TestWhoSettingsApiSees:
    """The preferences dependencies themselves, on a route that depends on nothing else.

    Every real route also depends on the caller, so a 401 there proves nothing about the
    preferences dependency: these probes take only it, and ask for the floor whatever they
    are given, so a dependency that stopped verifying the token first would show settings-api
    a forged one here.
    """

    @pytest.fixture
    async def probed(
        self, keyring: FakeKeyring, settings_api: FakeSettingsClient
    ) -> AsyncIterator[AsyncClient]:
        app = create_app(
            build_settings(service_tokens={"lucy-api": SERVICE_TOKEN}),
            transport=keyring.transport(),
            preferences=SettingsApiPreferences(client=settings_api),
        )

        async def person(preferences: PreferencesDep) -> int:
            return await preferences.importance_floor()

        async def sibling(preferences: ServicePreferencesDep) -> int:
            return await preferences.importance_floor()

        app.add_api_route("/probe/person", person)
        app.add_api_route("/probe/sibling", sibling)
        async with (
            LifespanManager(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            yield client

    async def test_the_probes_reach_settings_api_with_a_verified_token(
        self, probed: AsyncClient, settings_api: FakeSettingsClient
    ) -> None:
        person = await probed.get("/probe/person", headers=bearer())
        sibling = await probed.get("/probe/sibling", headers=internal_headers())

        assert person.json() == sibling.json() == 4
        assert settings_api.resolves == 2

    async def test_a_forged_token_never_reaches_settings_api_on_either_surface(
        self, probed: AsyncClient, settings_api: FakeSettingsClient
    ) -> None:
        """The bug, named: the old test of this only drove routes that depend on the caller
        themselves, so it went on passing with a preferences dependency that no longer did,
        and that dependency would then show settings-api any token it was handed."""
        token = forged()

        person = await probed.get("/probe/person", headers={"Authorization": f"Bearer {token}"})
        sibling = await probed.get("/probe/sibling", headers=internal_headers(token))

        assert person.status_code == sibling.status_code == 401
        assert settings_api.resolves == 0


class TestNobodyChose:
    @pytest.mark.parametrize("settings_api", [FakeSettingsClient({NAMESPACE: {}})])
    async def test_without_a_floor_the_least_important_memory_is_still_kept(
        self, http: AsyncClient
    ) -> None:
        response = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Had toast", "importance": 1}
        )

        assert response.status_code == 201

    async def test_without_settings_api_the_least_important_memory_is_still_kept(
        self, client: AsyncClient
    ) -> None:
        """The bug, named: turning per-person settings on must not change what a deployment
        that never configured them keeps."""
        response = await client.post(
            "/v1/memory", headers=bearer(), json={"title": "Had toast", "importance": 1}
        )

        assert response.status_code == 201


class TestCorrecting:
    async def test_a_correction_below_the_floor_still_replaces_what_it_corrects(
        self, http: AsyncClient
    ) -> None:
        """The bug, named: refusing a low-importance correction would leave the claim it
        corrects standing as the truth, which is worse than remembering one row too many."""
        original = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Home city", "body": "London"}
        )
        corrected = await http.post(
            f"/v1/memory/{original.json()['id']}/correct",
            headers=bearer(),
            json={"title": "Home city", "body": "Bristol", "importance": 1},
        )

        assert corrected.status_code == 201, corrected.text
        found = await http.get("/v1/memory/search?q=city", headers=bearer())
        assert [row["body"] for row in found.json()["data"]] == ["Bristol"]


class TestRepeating:
    """A write that repeats a remembered claim revises it and adds nothing new."""

    async def test_a_repeat_below_the_floor_downgrades_and_retires_what_is_remembered(
        self, http: AsyncClient, settings_api: FakeSettingsClient
    ) -> None:
        """The bug, named: a repeat is a revision of the memory it duplicates, but the floor
        treated it as a new memory and refused it with a 422 saying nothing was stored -- so
        lowering the importance of, or setting an expiry on, something already remembered
        was refused, and the floor made memory-api keep more about the person, not less."""
        original = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Home city", "importance": 8}
        )
        asked = settings_api.resolves
        repeat = {"title": "Home city", "importance": 1, "expires_at": 4_000_000_000}

        public = await http.post("/v1/memory", headers=bearer(), json=repeat)
        internal = await http.post(
            "/v1/internal/memory", headers=internal_headers(), json={**repeat, "importance": 2}
        )

        assert public.status_code == internal.status_code == 201, public.text
        assert public.json()["id"] == internal.json()["id"] == original.json()["id"]
        assert public.json()["importance"] == 1
        assert public.json()["expires_at"] == 4_000_000_000
        assert internal.json()["importance"] == 2
        # A repeat adds nothing, so the floor is never even asked for.
        assert settings_api.resolves == asked

    async def test_a_batch_add_that_repeats_is_applied_and_only_a_new_one_is_named(
        self, http: AsyncClient
    ) -> None:
        await http.post("/v1/memory", headers=bearer(), json={"title": "Home city"})
        repeating = {"action": "ADD", "memory": {"title": "Home city", "importance": 1}}
        adding = {"action": "ADD", "memory": {"title": "Had toast", "importance": 1}}

        refused = await http.post(
            "/v1/memory/batch", headers=bearer(), json={"decisions": [repeating, adding]}
        )
        applied = await http.post(
            "/v1/memory/batch", headers=bearer(), json={"decisions": [repeating]}
        )

        assert refused.status_code == 422
        assert ": 1." in refused.json()["detail"]
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"][0]["importance"] == 1
        assert await stored(http) == ["Home city"]

    async def test_a_repeat_carries_on_through_an_outage(
        self, http: AsyncClient, settings_api: FakeSettingsClient
    ) -> None:
        await http.post("/v1/memory", headers=bearer(), json={"title": "Home city"})
        settings_api.unavailable = True

        repeat = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Home city", "importance": 1}
        )
        new = await http.post("/v1/memory", headers=bearer(), json={"title": "Work city"})

        assert repeat.status_code == 201, repeat.text
        assert new.status_code == 503

    async def test_repeating_a_forgotten_memory_is_a_new_one_and_held_to_the_floor(
        self, http: AsyncClient
    ) -> None:
        original = await http.post("/v1/memory", headers=bearer(), json={"title": "Home city"})
        await http.post(f"/v1/memory/{original.json()['id']}/forget", headers=bearer())

        response = await http.post(
            "/v1/memory", headers=bearer(), json={"title": "Home city", "importance": 1}
        )

        assert response.status_code == 422
        assert response.json()["type"] == PROBLEMS + "below-importance-floor"


class TestBatches:
    async def test_one_addition_below_the_floor_refuses_the_whole_batch(
        self, http: AsyncClient
    ) -> None:
        """The bug, named: a batch is all or nothing, and quietly dropping one decision would
        break `data[i]` being decision `i`'s memory."""
        response = await http.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={
                "decisions": [
                    {"action": "ADD", "memory": {"title": "Allergic to nuts", "importance": 9}},
                    {"action": "ADD", "memory": {"title": "Had toast", "importance": 1}},
                ]
            },
        )

        assert response.status_code == 422
        assert response.json()["type"] == PROBLEMS + "below-importance-floor"
        assert ": 1." in response.json()["detail"]
        assert await stored(http) == []

    async def test_an_update_below_the_floor_is_a_correction_and_applies(
        self, http: AsyncClient
    ) -> None:
        original = await http.post("/v1/memory", headers=bearer(), json={"title": "Home city"})
        response = await http.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={
                "decisions": [
                    {
                        "action": "UPDATE",
                        "memory_id": original.json()["id"],
                        "memory": {"title": "Home city", "body": "Bristol", "importance": 1},
                    }
                ]
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"][0]["body"] == "Bristol"


class TestAnOutage:
    @pytest.fixture
    def settings_api(self) -> FakeSettingsClient:
        fake = FakeSettingsClient()
        fake.unavailable = True
        return fake

    async def test_a_new_memory_waits_rather_than_being_written_on_a_guess(
        self, http: AsyncClient
    ) -> None:
        """The bug, named: writing during an outage on an assumed floor writes down more
        about a person than they may have agreed to, and that cannot be taken back."""
        public = await http.post("/v1/memory", headers=bearer(), json={"title": "Home city"})
        internal = await http.post(
            "/v1/internal/memory", headers=internal_headers(), json={"title": "Home city"}
        )
        batch = await http.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={"decisions": [{"action": "ADD", "memory": {"title": "Home city"}}]},
        )

        for response in (public, internal, batch):
            assert response.status_code == 503
            assert response.json()["type"] == PROBLEMS + "preferences-unavailable"
        assert await stored(http) == []

    async def test_reading_forgetting_and_a_batch_with_no_addition_carry_on(
        self, http: AsyncClient
    ) -> None:
        """The bug, named: an operation that never touches the floor must not fail for it."""
        search = await http.get("/v1/memory/search", headers=bearer())
        batch = await http.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={"decisions": [{"action": "DELETE", "memory_id": "mem_absent"}]},
        )

        assert search.status_code == 200
        # The batch got as far as the store, which is where an unknown id is a 404.
        assert batch.status_code == 404


class TestARefusal:
    @pytest.fixture
    def settings_api(self) -> FakeSettingsClient:
        fake = FakeSettingsClient()
        fake.rejects[NAMESPACE] = (403, "memory-api is not granted memory")
        return fake

    async def test_a_missing_grant_is_a_503_that_names_neither_grant_nor_namespace(
        self, http: AsyncClient
    ) -> None:
        response = await http.post("/v1/memory", headers=bearer(), json={"title": "Home city"})

        assert response.status_code == 503
        assert response.json()["type"] == PROBLEMS + "preferences-unavailable"
        assert "granted" not in response.text

    async def test_a_batch_with_no_addition_never_asks_settings_api(
        self, http: AsyncClient, settings_api: FakeSettingsClient
    ) -> None:
        """The bug, named: the batch's preferences dependency asked settings-api for every
        batch, so a refused grant -- or a slow settings-api -- failed or held up a batch of
        corrections and forgets that the floor has no say in."""
        adding = await http.post(
            "/v1/internal/memory", headers=internal_headers(), json={"title": "Home city"}
        )
        assert adding.status_code == 503  # A new memory does need the floor.
        settings_api.resolves = 0

        batch = await http.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={
                "decisions": [
                    {"action": "DELETE", "memory_id": "mem_absent"},
                    {"action": "NOOP", "memory_id": "mem_absent"},
                ]
            },
        )

        # The batch got as far as the store, which is where an unknown id is a 404.
        assert batch.status_code == 404
        assert settings_api.resolves == 0


async def test_shutting_down_closes_the_settings_api_client(keyring: FakeKeyring) -> None:
    """The bug, named: a client left open at shutdown leaks its connection pool, and the
    store's thread must still be retired whatever it does on the way out."""

    class Recording(FakeSettingsClient):
        closed = False

        async def aclose(self) -> None:
            Recording.closed = True

    app = create_app(
        build_settings(),
        transport=keyring.transport(),
        preferences=SettingsApiPreferences(client=Recording()),
    )
    async with LifespanManager(app):
        pass

    assert Recording.closed
