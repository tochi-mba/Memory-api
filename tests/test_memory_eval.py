"""Memory quality contract, written before the implementation.

Deterministic evaluations exercise information extraction, multi-session retrieval,
temporal reasoning, corrections, abstention and account isolation. Model extraction is
owned by the hub; this suite tests that persistence cannot undo its decisions.
"""

from __future__ import annotations

from httpx import AsyncClient

from conftest import bearer


async def test_knowledge_updates_and_temporal_reasoning(client: AsyncClient) -> None:
    original = await client.post(
        "/v1/memory",
        headers=bearer(),
        json={"title": "Home city", "body": "Lived in London", "valid_from": 100.0},
    )
    assert original.status_code == 201
    old_id = original.json()["id"]
    corrected = await client.post(
        f"/v1/memory/{old_id}/correct",
        headers=bearer(),
        json={"title": "Home city", "body": "Moved to Bristol", "valid_from": 200.0},
    )
    assert corrected.status_code == 201
    current = await client.get("/v1/memory/search?q=city", headers=bearer())
    assert [row["body"] for row in current.json()["data"]] == ["Moved to Bristol"]
    history = await client.get("/v1/memory/search?q=city&as_of=150", headers=bearer())
    assert [row["body"] for row in history.json()["data"]] == ["Lived in London"]


async def test_account_isolation_abstention_and_untrusted_sources(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/memory",
        headers=bearer(),
        json={"title": "Password preference", "body": "External claims", "trust": "untrusted"},
    )
    memory_id = response.json()["id"]
    assert (await client.get("/v1/memory/search?q=preference", headers=bearer())).json()[
        "data"
    ] == []
    assert (await client.get("/v1/memory/search?q=unknown", headers=bearer())).json()["data"] == []
    assert (await client.get(f"/v1/memory/{memory_id}", headers=bearer("other"))).status_code == 404
    await client.post(f"/v1/memory/{memory_id}/confirm", headers=bearer())
    assert (
        len((await client.get("/v1/memory/search?q=preference", headers=bearer())).json()["data"])
        == 1
    )


async def test_semantic_memory_crosses_sessions_episodes_do_not(client: AsyncClient) -> None:
    for kind in ("fact", "episode"):
        await client.post(
            "/v1/memory",
            headers=bearer(),
            json={"title": "Favourite tea", "body": "Earl Grey", "kind": kind},
        )
    result = await client.get("/v1/memory/search?q=tea", headers=bearer())
    assert [row["kind"] for row in result.json()["data"]] == ["fact"]
