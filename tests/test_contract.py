"""The HTTP contract, pinned.

The twenty-one ``operation_id``s are MCP tool names: renaming one breaks every client with
a tool bound to it, so they are checked as an exact set rather than a minimum. The
structural assertions are the isolation invariant read off the generated schema rather than
off a comment -- no path and no parameter anywhere names an account.

A schema test is cheap and catches the class of change nothing else does: a route added
without an operation id, a body model that quietly stopped forbidding extra fields, a
parameter that started accepting somebody else's id.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import build_settings
from memory_api.api.app import create_app
from memory_api.api.routers import memory

OPERATIONS = frozenset(
    {
        "check_liveness",
        "check_readiness",
        "whoami",
        "create_memory",
        "list_memories",
        "search_memories",
        "list_memory_blocks",
        "get_memory_block",
        "write_memory_block",
        "delete_memory_block",
        "list_memory_topics",
        "get_memory_topic",
        "update_memory_topic",
        "reconcile_memories",
        "forget_all_memories",
        "get_memory",
        "correct_memory",
        "confirm_memory",
        "forget_memory",
        "restore_memory",
        "delete_memory",
    }
)

FORBIDDEN_NAMES = {"account_id", "account", "user_id", "subject", "sub", "asserted_by"}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    document: dict[str, Any] = create_app(build_settings()).openapi()
    return document


def operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    ]


def test_the_exact_set_of_twenty_one_operation_ids(schema: dict[str, Any]) -> None:
    found = {operation["operationId"] for _, _, operation in operations(schema)}
    assert found == OPERATIONS, (
        "operation_ids are MCP tool names and public API: renaming one breaks every "
        f"client with a tool bound to it. Difference: {found ^ OPERATIONS}"
    )


class TestEveryOperation:
    def test_has_an_explicit_id_rather_than_one_fastapi_generated(
        self, schema: dict[str, Any]
    ) -> None:
        # FastAPI's generated ids carry the function name, the path and the method, so they
        # change when any of those is refactored -- which for an MCP tool name is a break.
        for path, method, operation in operations(schema):
            assert not operation["operationId"].endswith(f"_{method}"), (path, method)

    def test_has_a_summary_and_a_longer_description(self, schema: dict[str, Any]) -> None:
        # Both are read by a model deciding whether to call the tool. A summary with no
        # description means it decides from a handful of words.
        for _, _, operation in operations(schema):
            assert operation["summary"].strip(), operation["operationId"]
            assert len(operation["description"]) > len(operation["summary"]), operation[
                "operationId"
            ]

    def test_documents_a_response_for_every_failure_it_can_produce(
        self, schema: dict[str, Any]
    ) -> None:
        expected_failures = {
            "create_memory": {"401", "422"},
            "list_memories": {"401", "422"},
            "search_memories": {"401", "422"},
            "get_memory": {"401", "404", "422"},
            "correct_memory": {"401", "404", "409", "422"},
            "confirm_memory": {"401", "404", "422"},
            "forget_memory": {"401", "404", "422"},
            "restore_memory": {"401", "404", "422"},
            "delete_memory": {"401", "404", "422"},
            "get_memory_block": {"401", "404", "422"},
            "write_memory_block": {"401", "422"},
            "delete_memory_block": {"401", "422"},
            "reconcile_memories": {"401", "404", "409", "422"},
            "forget_all_memories": {"401"},
            "list_memory_topics": {"401", "422"},
            "get_memory_topic": {"401", "404", "422"},
            "update_memory_topic": {"401", "404", "422"},
            "check_readiness": {"503"},
        }
        for _, _, operation in operations(schema):
            wanted = expected_failures.get(operation["operationId"], set())
            assert wanted <= set(operation["responses"]), operation["operationId"]

    def test_no_path_or_parameter_names_an_account(self, schema: dict[str, Any]) -> None:
        # The isolation invariant, against the generated contract. A cross-account read is
        # not forbidden here; it is inexpressible.
        for path, _, operation in operations(schema):
            segments = {segment.strip("{}") for segment in path.split("/")}
            assert not (segments & FORBIDDEN_NAMES), path
            for parameter in operation.get("parameters", []):
                assert parameter["name"] not in FORBIDDEN_NAMES, (path, parameter["name"])


class TestTheErrorShape:
    def test_every_documented_failure_is_a_problem_document(self, schema: dict[str, Any]) -> None:
        # One error shape for the whole service, so a client -- or a model calling this as a
        # tool -- has exactly one thing to parse. Readiness is the deliberate exception: its
        # 503 is a report of which dependency is down, which is the point of asking.
        for _, _, operation in operations(schema):
            if operation["operationId"] == "check_readiness":
                continue
            for code, response in operation["responses"].items():
                if not code.startswith(("2", "3")) and "content" in response:
                    body = response["content"]["application/json"]["schema"]
                    assert body["$ref"].endswith("/Problem"), (operation["operationId"], code)

    def test_readiness_answers_its_503_with_the_same_report_as_its_200(
        self, schema: dict[str, Any]
    ) -> None:
        by_id = {operation["operationId"]: operation for _, _, operation in operations(schema)}
        failure = by_id["check_readiness"]["responses"]["503"]["content"]
        assert failure["application/json"]["schema"]["$ref"].endswith("/ReadyResponse")

    def test_the_problem_model_carries_the_five_rfc_9457_fields_and_a_request_id(
        self, schema: dict[str, Any]
    ) -> None:
        problem = schema["components"]["schemas"]["Problem"]
        assert set(problem["required"]) == {"type", "title", "status", "detail"}
        assert "request_id" in problem["properties"]


class TestRequestBodies:
    def test_every_request_body_forbids_extra_fields(self, schema: dict[str, Any]) -> None:
        # This is where the isolation invariant would leak if it ever leaked: a body model
        # that accepts unknown fields accepts `account_id` and silently ignores it, which
        # reads to the caller exactly like it worked.
        named = {"MemoryInput", "BlockInput", "Batch", "Decision", "TopicUpdate"}
        for name in named:
            component = schema["components"]["schemas"][name]
            assert component.get("additionalProperties") is False, name


def test_the_literal_paths_are_declared_before_the_parameterised_one() -> None:
    """Route order, read off the router rather than off a comment.

    Within one router, Starlette still matches in declaration order: a ``/{memory_id}``
    declared first swallows ``/search`` and answers "no memory called search". Nothing
    raises when that happens -- the literal route simply stops being reachable -- which is
    the kind of break that ships.

    This is checked on ``memory.router`` and not on the assembled app on purpose. FastAPI
    0.141 ranks an included router's literal paths ahead of another router's parameterised
    ones, so the *cross*-router ordering in ``routers/__init__.py`` is currently belt as
    well as braces. Declaration order inside a single router has no such safety net, and
    that is what this pins.
    """
    paths = [getattr(route, "path", "") for route in memory.router.routes]
    swallower = paths.index("/v1/memory/{memory_id}")
    for literal in ("/v1/memory/search", "/v1/memory/blocks", "/v1/memory/batch"):
        assert paths.index(literal) < swallower, literal

    # And the block routes, which sit under `/blocks/{label}` rather than under `/{id}`.
    assert paths.index("/v1/memory/blocks") < paths.index("/v1/memory/blocks/{label}")
