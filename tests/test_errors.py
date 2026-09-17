"""Every failure, in one shape.

A client -- or a model calling this as a tool -- should have exactly one error document to
parse, whether the failure came from a domain rule, from FastAPI's validation, from
Starlette's router or from a bug. These tests check that the shape holds at each of those
four sources, because each is produced by a different handler and it is entirely possible
for three of them to agree and the fourth to answer ``{"detail": ...}``.

The one that matters most is the last: ``detail`` must never echo what the caller sent. A
422 body is logged by the caller, shown in a transcript and often handed back to a model,
and on this service the thing that failed validation is a sentence somebody wrote about
their own life -- or the credential the secret check just refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager
from keyring_client.testing import FakeKeyring

from conftest import bearer
from memory_api.api.errors import PROBLEM_BASE_URI, RETRY_AFTER_HEADER
from memory_api.api.middleware import REQUEST_ID_HEADER
from memory_api.api.schemas import PROBLEM_CONTENT_TYPE
from memory_api.auth.verifier import KEYS_UNAVAILABLE
from memory_api.core.config import Settings

if TYPE_CHECKING:
    from httpx import AsyncClient


def assert_is_a_problem(response: httpx.Response, status_code: int, slug: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert body["type"] == f"{PROBLEM_BASE_URI}/{slug}"
    assert body["status"] == status_code
    assert body["title"]
    assert body["detail"]
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


class TestTheFourSourcesOfFailure:
    async def test_a_domain_refusal_is_a_problem_at_the_status_the_error_declares(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/memory/mem_nope", headers=bearer())
        assert_is_a_problem(response, 404, "not-found")

    async def test_a_validation_failure_is_a_problem_with_per_field_detail(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/v1/memory", headers=bearer(), json={"title": ""})
        assert_is_a_problem(response, 422, "validation-failed")
        assert response.json()["errors"][0]["location"] == "body.title"

    async def test_an_unrouted_path_is_a_problem_rather_than_starlettes_own_body(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/nothing-here")
        assert_is_a_problem(response, 404, "not-found")

    async def test_a_method_the_route_does_not_have_is_a_problem(self, client: AsyncClient) -> None:
        response = await client.put("/v1/memory", headers=bearer(), json={})
        assert_is_a_problem(response, 405, "method-not-allowed")

    async def test_a_missing_token_is_a_problem(self, client: AsyncClient) -> None:
        assert_is_a_problem(await client.get("/v1/memory"), 401, "unauthorized")

    async def test_a_refused_token_is_a_problem(self, client: AsyncClient) -> None:
        response = await client.get("/v1/memory", headers={"Authorization": "Bearer not-a-token"})
        assert_is_a_problem(response, 401, "unauthorized")

    async def test_a_credential_refusal_carries_its_own_slug(self, client: AsyncClient) -> None:
        # The domain error's `code` becomes the problem type, so a client can branch on
        # "you tried to store a secret" without parsing English.
        response = await client.post(
            "/v1/memory",
            headers=bearer(),
            json={"title": "API key", "body": "sk-" + "a" * 24},
        )
        assert_is_a_problem(response, 422, "credential-refused")

    async def test_a_conflict_carries_its_own_slug(self, client: AsyncClient) -> None:
        created = await client.post(
            "/v1/memory", headers=bearer(), json={"title": "Home city", "body": "London"}
        )
        memory_id = created.json()["id"]
        for _ in range(2):
            response = await client.post(
                f"/v1/memory/{memory_id}/correct",
                headers=bearer(),
                json={"title": "Home city", "body": "Bristol"},
            )
        assert_is_a_problem(response, 409, "conflict")


class TestADetailNeverEchoesWhatWasSent:
    async def test_a_rejected_memory_does_not_come_back_in_the_response(
        self, client: AsyncClient
    ) -> None:
        sentinel = "a-very-private-thing-nobody-should-log"
        response = await client.post(
            "/v1/memory",
            headers=bearer(),
            json={"title": sentinel, "body": "x", "confidence": 99},
        )
        assert response.status_code == 422
        assert sentinel not in response.text

    async def test_a_rejected_block_does_not_come_back_either(self, client: AsyncClient) -> None:
        sentinel = "another-private-thing"
        response = await client.put(
            "/v1/memory/blocks/persona",
            headers=bearer(),
            json={"body": sentinel, "char_limit": 1},
        )
        assert response.status_code == 422
        assert sentinel not in response.text

    async def test_a_rejected_query_parameter_does_not_come_back(self, client: AsyncClient) -> None:
        response = await client.get("/v1/memory?limit=not-a-number", headers=bearer())
        assert response.status_code == 422
        assert "not-a-number" not in response.text


class TestTheRequestId:
    async def test_every_response_carries_one(self, client: AsyncClient) -> None:
        response = await client.get("/healthy")
        assert response.headers[REQUEST_ID_HEADER]

    async def test_two_requests_get_different_ids(self, client: AsyncClient) -> None:
        first = await client.get("/healthy")
        second = await client.get("/healthy")
        assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]

    async def test_an_id_supplied_by_the_caller_is_honoured_so_a_trace_can_span_services(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/healthy", headers={REQUEST_ID_HEADER: "trace-abc"})
        assert response.headers[REQUEST_ID_HEADER] == "trace-abc"

    async def test_a_supplied_id_is_length_capped_because_it_reaches_every_log_record(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/healthy", headers={REQUEST_ID_HEADER: "x" * 500})
        assert len(response.headers[REQUEST_ID_HEADER]) == 64


class TestAnUnexpectedExceptionIsNotAnInformationLeak:
    async def test_it_becomes_a_500_that_withholds_the_message_but_keeps_the_id(
        self, client: AsyncClient
    ) -> None:
        # Raised from a dependency rather than from a route, so nothing about the route's
        # own error handling can catch it first. The message is the thing under test: it
        # must not survive into the response.
        from memory_api.api.dependencies import get_container

        secret_message = "connection to 10.0.0.4 failed for acct_example"

        def explode() -> object:
            raise RuntimeError(secret_message)

        app = client._transport.app  # type: ignore[attr-defined]
        app.dependency_overrides[get_container] = explode
        try:
            response = await client.get("/healthy")
        finally:
            app.dependency_overrides.clear()

        assert_is_a_problem(response, 500, "internal-server-error")
        assert secret_message not in response.text
        assert "quote the request id" in response.json()["detail"]


class TestKeyringBeingDownIsNotTheCallersFault:
    async def test_it_answers_503_with_a_retry_after_rather_than_401(
        self, settings: Settings, keyring: FakeKeyring
    ) -> None:
        # A 401 would send somebody through a login they do not need, for a key *we* could
        # not fetch, and logging in again would not fix it.
        from memory_api.api.app import create_app

        keyring.error = httpx.ConnectError("down")
        app = create_app(settings, transport=keyring.transport())
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http,
        ):
            response = await http.get("/v1/memory", headers=bearer())

        assert_is_a_problem(response, 503, "keyring-unreachable")
        assert response.headers[RETRY_AFTER_HEADER] == "5"
        assert response.json()["detail"] == KEYS_UNAVAILABLE


class TestReadinessReportsTheDatabaseToo:
    async def test_a_healthy_database_is_reported_as_reachable(self, client: AsyncClient) -> None:
        checks = (await client.get("/ready")).json()["checks"]
        assert checks["database"] == {"status": "ok", "detail": {"reachable": True, "reason": None}}

    async def test_an_unusable_database_takes_the_instance_out_of_rotation(
        self, client: AsyncClient
    ) -> None:
        # Closing the connection under the worker is the cheapest way to produce the
        # failure an operator actually sees: a file that has gone away beneath a live
        # process. Every /v1 route would fail, so /ready has to say so.
        app = client._transport.app  # type: ignore[attr-defined]
        await app.state.container.store.call(lambda handle: handle.close())

        response = await client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"]["status"] == "degraded"
        # The type name, never the message: a sqlite error carries the path of the file.
        assert body["checks"]["database"]["detail"]["reason"] == "ProgrammingError"


@pytest.mark.parametrize("path", ["/healthy", "/ready", "/v1/whoami"])
async def test_a_successful_response_is_not_dressed_up_as_a_problem(
    client: AsyncClient, path: str
) -> None:
    response = await client.get(path, headers=bearer())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "type" not in response.json()
