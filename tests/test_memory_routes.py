"""The memory routes, over HTTP.

What these pin is the surface rather than the storage: which literal paths survive sitting
underneath ``/{memory_id}``, that identity comes from the token and from nowhere else, and
that the two list views really are two views. The store's own behaviour is tested a layer
down, in ``test_store.py``.

The account isolation tests all take the same shape -- write as one account, read as
another, expect 404 -- because that is the property the service exists to keep, and it has
to hold on every route rather than on the ones somebody remembered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import ACCOUNT, OTHER_ACCOUNT, bearer

if TYPE_CHECKING:
    from httpx import AsyncClient


async def write(client: AsyncClient, **fields: object) -> dict[str, object]:
    """Write one memory as the default account and return it."""
    response = await client.post(
        "/v1/memory", headers=bearer(), json={"title": "Home city", **fields}
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


class TestRouteOrder:
    """Starlette matches in declaration order; these literal paths sit under ``/{memory_id}``.

    Each of these would still return 200-shaped JSON if the routing were wrong -- a 404 body
    saying "Memory not found" -- so every test reads a field only the right route produces.
    """

    async def test_search_is_the_search_and_not_a_memory_called_search(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/memory/search", headers=bearer())
        assert response.status_code == 200
        assert "has_more" in response.json()

    async def test_blocks_is_the_block_list_and_not_a_memory_called_blocks(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/memory/blocks", headers=bearer())
        assert response.status_code == 200
        assert response.json() == {"data": []}

    async def test_topics_is_the_index_and_not_a_memory_called_topics(
        self, client: AsyncClient
    ) -> None:
        # This one is not a matter of declaration order within a file: the topic routes are
        # a separate router, and the ordering that saves them is in `routers/__init__.py`.
        response = await client.get("/v1/memory/topics", headers=bearer())
        assert response.status_code == 200
        assert "total" in response.json()

    async def test_batch_is_the_batch_and_not_a_memory_called_batch(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={"decisions": [{"action": "ADD", "memory": {"title": "Favourite tea"}}]},
        )
        assert response.status_code == 200
        assert len(response.json()["data"]) == 1

    async def test_a_memory_id_still_reaches_the_memory_route(self, client: AsyncClient) -> None:
        memory = await write(client, body="Lived in London")
        response = await client.get(f"/v1/memory/{memory['id']}", headers=bearer())
        assert response.json()["body"] == "Lived in London"


class TestWhoTheMemoryBelongsTo:
    async def test_the_account_comes_from_the_token_and_not_from_the_body(
        self, client: AsyncClient
    ) -> None:
        # There is nowhere to put another account's id, so this is a 422 rather than a
        # write charged to somebody else. That is the property, stated as a test.
        response = await client.post(
            "/v1/memory",
            headers=bearer(),
            json={"title": "Home city", "account_id": OTHER_ACCOUNT},
        )
        assert response.status_code == 422

    async def test_asserted_by_is_taken_from_the_token_and_source_is_left_as_claimed(
        self, client: AsyncClient
    ) -> None:
        # `source` is what the caller says; `asserted_by` is what the service knows. A
        # caller that could set the second could forge provenance for anything.
        memory = await write(client, source="the doctor")
        assert memory["source"] == "the doctor"
        assert memory["asserted_by"] == ACCOUNT

    async def test_asserted_by_cannot_be_supplied(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/memory", headers=bearer(), json={"title": "Home city", "asserted_by": "someone"}
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/v1/memory/{id}"),
            ("post", "/v1/memory/{id}/correct"),
            ("post", "/v1/memory/{id}/confirm"),
            ("post", "/v1/memory/{id}/forget"),
            ("post", "/v1/memory/{id}/restore"),
            ("delete", "/v1/memory/{id}"),
        ],
    )
    async def test_another_account_cannot_reach_a_memory_by_id(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        memory = await write(client, body="Lived in London")
        body = {"title": "Home city"} if path.endswith("correct") else None
        response = await client.request(
            method, path.format(id=memory["id"]), headers=bearer(OTHER_ACCOUNT), json=body
        )
        assert response.status_code == 404

    async def test_a_memory_written_by_one_account_is_invisible_to_another(
        self, client: AsyncClient
    ) -> None:
        await write(client, body="Lived in London")
        response = await client.get("/v1/memory", headers=bearer(OTHER_ACCOUNT))
        assert response.json()["data"] == []


class TestTheTwoListViews:
    async def test_the_full_view_shows_untrusted_memories_and_their_provenance(
        self, client: AsyncClient
    ) -> None:
        await write(client, body="External claims", trust="untrusted")
        listed = (await client.get("/v1/memory", headers=bearer())).json()["data"]
        assert [row["trust"] for row in listed] == ["untrusted"]
        assert listed[0]["asserted_by"] == ACCOUNT

    async def test_the_retrieval_view_does_not(self, client: AsyncClient) -> None:
        # The difference between the two is a security boundary rather than a preference:
        # one is what a person is shown, the other is what goes into a prompt.
        await write(client, body="External claims", trust="untrusted")
        assert (await client.get("/v1/memory/search", headers=bearer())).json()["data"] == []

    async def test_a_filter_the_selection_does_not_have_is_refused_rather_than_ignored(
        self, client: AsyncClient
    ) -> None:
        # A misspelled filter that is silently dropped turns "these are the ones that got
        # through a typo" into "these are all your memories".
        response = await client.get("/v1/memory?includ_forgotten=true", headers=bearer())
        assert response.status_code == 422

    async def test_the_selection_model_is_what_the_query_string_speaks(
        self, client: AsyncClient
    ) -> None:
        await write(client, body="Lived in London")
        forgotten = await write(client, title="Old note", body="Delete me")
        await client.post(f"/v1/memory/{forgotten['id']}/forget", headers=bearer())
        hidden = await client.get("/v1/memory?include_forgotten=false", headers=bearer())
        shown = await client.get("/v1/memory?include_forgotten=true&limit=1", headers=bearer())
        assert len(hidden.json()["data"]) == 1
        assert shown.json()["has_more"] is True

    async def test_a_limit_outside_the_models_bounds_is_refused(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/memory?limit=0", headers=bearer())).status_code == 422
        assert (await client.get("/v1/memory?limit=500", headers=bearer())).status_code == 422


class TestTheSingleMemoryRoutes:
    async def test_a_memory_that_does_not_exist_is_not_found(self, client: AsyncClient) -> None:
        response = await client.get("/v1/memory/mem_nope", headers=bearer())
        assert response.status_code == 404

    async def test_correcting_answers_201_with_the_new_memory(self, client: AsyncClient) -> None:
        # 201 because a correction creates something. The old memory is retired, not edited,
        # which is what makes the history readable afterwards.
        original = await write(client, body="Lived in London")
        response = await client.post(
            f"/v1/memory/{original['id']}/correct",
            headers=bearer(),
            json={"title": "Home city", "body": "Moved to Bristol"},
        )
        assert response.status_code == 201
        assert response.json()["supersedes_id"] == original["id"]

    async def test_correcting_an_already_corrected_memory_is_a_conflict(
        self, client: AsyncClient
    ) -> None:
        original = await write(client, body="Lived in London")
        for _ in range(2):
            response = await client.post(
                f"/v1/memory/{original['id']}/correct",
                headers=bearer(),
                json={"title": "Home city", "body": "Moved to Bristol"},
            )
        assert response.status_code == 409
        assert response.json()["type"].endswith("/conflict")

    async def test_forget_and_restore_are_a_round_trip(self, client: AsyncClient) -> None:
        memory = await write(client, body="Lived in London")
        forgotten = await client.post(f"/v1/memory/{memory['id']}/forget", headers=bearer())
        assert forgotten.json()["forgotten_at"] is not None
        restored = await client.post(f"/v1/memory/{memory['id']}/restore", headers=bearer())
        assert restored.json()["forgotten_at"] is None

    async def test_delete_forgets_rather_than_erasing_and_answers_no_content(
        self, client: AsyncClient
    ) -> None:
        memory = await write(client, body="Lived in London")
        response = await client.delete(f"/v1/memory/{memory['id']}", headers=bearer())
        assert response.status_code == 204
        assert response.content == b""
        still_there = await client.get(f"/v1/memory/{memory['id']}", headers=bearer())
        assert still_there.json()["forgotten_at"] is not None

    async def test_confirming_lets_an_untrusted_memory_into_retrieval(
        self, client: AsyncClient
    ) -> None:
        memory = await write(client, title="Allergy", body="Reported", trust="untrusted")
        absent = await client.get("/v1/memory/search?q=Allergy", headers=bearer())
        assert absent.json()["data"] == [], "unusable until somebody vouches for it"

        confirmed = await client.post(f"/v1/memory/{memory['id']}/confirm", headers=bearer())
        assert confirmed.json()["confirmed_at"] is not None
        assert confirmed.json()["trust"] == "untrusted", "still says where it came from"

        found = await client.get("/v1/memory/search?q=Allergy", headers=bearer())
        assert len(found.json()["data"]) == 1


class TestBlocks:
    async def test_a_block_is_written_read_listed_and_removed(self, client: AsyncClient) -> None:
        written = await client.put(
            "/v1/memory/blocks/persona", headers=bearer(), json={"body": "Warm and brief"}
        )
        assert written.status_code == 200
        read = await client.get("/v1/memory/blocks/persona", headers=bearer())
        assert read.json()["body"] == "Warm and brief"
        listed = await client.get("/v1/memory/blocks", headers=bearer())
        assert [block["label"] for block in listed.json()["data"]] == ["persona"]
        removed = await client.delete("/v1/memory/blocks/persona", headers=bearer())
        assert removed.status_code == 204
        assert (await client.get("/v1/memory/blocks/persona", headers=bearer())).status_code == 404

    async def test_a_block_longer_than_its_own_limit_is_refused(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/memory/blocks/persona",
            headers=bearer(),
            json={"body": "x" * 50, "char_limit": 10},
        )
        assert response.status_code == 422

    async def test_removing_a_block_that_is_not_there_still_succeeds(
        self, client: AsyncClient
    ) -> None:
        response = await client.delete("/v1/memory/blocks/persona", headers=bearer())
        assert response.status_code == 204

    async def test_another_accounts_block_is_not_found(self, client: AsyncClient) -> None:
        await client.put(
            "/v1/memory/blocks/persona", headers=bearer(), json={"body": "Warm and brief"}
        )
        response = await client.get("/v1/memory/blocks/persona", headers=bearer(OTHER_ACCOUNT))
        assert response.status_code == 404


class TestTheBatchRoute:
    async def test_every_decision_comes_back_in_the_order_it_was_sent(
        self, client: AsyncClient
    ) -> None:
        existing = await write(client, body="Lived in London")
        response = await client.post(
            "/v1/memory/batch",
            headers=bearer(),
            json={
                "decisions": [
                    {"action": "ADD", "memory": {"title": "Favourite tea", "body": "Earl Grey"}},
                    {"action": "NOOP", "memory_id": existing["id"]},
                ]
            },
        )
        assert [row["body"] for row in response.json()["data"]] == [
            "Earl Grey",
            "Lived in London",
        ]

    async def test_a_batch_naming_another_accounts_memory_is_not_found_and_writes_nothing(
        self, client: AsyncClient
    ) -> None:
        theirs = await write(client, body="Lived in London")
        response = await client.post(
            "/v1/memory/batch",
            headers=bearer(OTHER_ACCOUNT),
            json={
                "decisions": [
                    {"action": "ADD", "memory": {"title": "Favourite tea"}},
                    {"action": "NOOP", "memory_id": theirs["id"]},
                ]
            },
        )
        assert response.status_code == 404
        listed = await client.get("/v1/memory", headers=bearer(OTHER_ACCOUNT))
        assert listed.json()["data"] == []

    async def test_an_empty_batch_is_refused(self, client: AsyncClient) -> None:
        response = await client.post("/v1/memory/batch", headers=bearer(), json={"decisions": []})
        assert response.status_code == 422


class TestForgettingEverything:
    async def test_it_reports_how_many_memories_it_forgot_and_leaves_nothing_behind(
        self, client: AsyncClient
    ) -> None:
        await write(client, body="Lived in London")
        await write(client, title="Favourite tea", body="Earl Grey")
        await client.put(
            "/v1/memory/blocks/persona", headers=bearer(), json={"body": "Warm and brief"}
        )
        response = await client.delete("/v1/memory", headers=bearer())
        assert response.json() == {"forgotten": 2}
        assert (await client.get("/v1/memory", headers=bearer())).json()["data"] == []
        assert (await client.get("/v1/memory/blocks", headers=bearer())).json() == {"data": []}

    async def test_it_touches_only_the_account_whose_token_was_used(
        self, client: AsyncClient
    ) -> None:
        await write(client, body="Lived in London")
        assert (await client.delete("/v1/memory", headers=bearer(OTHER_ACCOUNT))).json() == {
            "forgotten": 0
        }
        assert len((await client.get("/v1/memory", headers=bearer())).json()["data"]) == 1


class TestCredentialsAreNotMemory:
    async def test_a_credential_shaped_body_is_refused_without_being_echoed(
        self, client: AsyncClient
    ) -> None:
        secret = "sk-" + "a" * 24
        response = await client.post(
            "/v1/memory", headers=bearer(), json={"title": "API key", "body": secret}
        )
        assert response.status_code == 422
        assert secret not in response.text

    async def test_a_credential_in_a_block_is_refused_too(self, client: AsyncClient) -> None:
        secret = "ghp_" + "b" * 24
        response = await client.put(
            "/v1/memory/blocks/persona", headers=bearer(), json={"body": secret}
        )
        assert response.status_code == 422
        assert secret not in response.text


class TestEveryRouteRequiresAToken:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/v1/memory"),
            ("get", "/v1/memory"),
            ("delete", "/v1/memory"),
            ("get", "/v1/memory/search"),
            ("get", "/v1/memory/blocks"),
            ("get", "/v1/memory/blocks/persona"),
            ("put", "/v1/memory/blocks/persona"),
            ("delete", "/v1/memory/blocks/persona"),
            ("post", "/v1/memory/batch"),
            ("get", "/v1/memory/mem_1"),
            ("post", "/v1/memory/mem_1/correct"),
            ("post", "/v1/memory/mem_1/confirm"),
            ("post", "/v1/memory/mem_1/forget"),
            ("post", "/v1/memory/mem_1/restore"),
            ("delete", "/v1/memory/mem_1"),
        ],
    )
    async def test_an_unauthenticated_request_is_refused(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        # `request` rather than `client.get`, because httpx refuses a body on a GET and
        # half of these routes are reads.
        response = await client.request(method, path, json={"title": "x", "body": "y"})
        assert response.status_code == 401
