"""The topic index: three operations under ``/v1/memory/topics``.

Its own module, and included ahead of the memory router, because ``/v1/memory/topics`` sits
underneath ``/v1/memory/{memory_id}``. FastAPI 0.141 happens to rank a literal path ahead of
another router's parameterised one, so the ordering is not currently what saves these routes
-- but relying on that is relying on a ranking rule nothing in this repo controls, and the
cost of registering them first is one line. The tests check the outcome, not the mechanism:
each topic route is asked for and has to answer as itself.

What these three operations are *for* is the point of the whole cluster layer. An assistant
carries the index -- one line per subject -- in its context every turn, and expands a topic
only once it has decided that topic is the one it needs. Forty lines instead of four
hundred, and the model can tell what it knows *about* rather than holding a flat pile of
sentences it cannot summarise.

Two rules make the index safe to put in a prompt, and both are enforced in the store rather
than here. A topic's counts are recomputed on every read, never stored, so the index cannot
overstate what is behind it. And a topic made entirely of untrusted memories does not appear
at all: its title was written from content somebody else supplied, and a title is the part
that reaches the prompt.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from memory_api.api.dependencies import CurrentCallerDep, StoreDep
from memory_api.api.schemas import Problem
from memory_api.domain.models import Topic, TopicDetail, TopicPage, TopicUpdate

router = APIRouter(prefix="/v1/memory/topics", tags=["topics"])

_PROBLEM: dict[str, Any] = {"model": Problem}
# See the note in `memory.py`: an explicit 422 keeps FastAPI from publishing its own
# validation-error document for a service that only ever sends problem documents.
_VALIDATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}

TopicIdPath = Annotated[str, Path(description="The topic's id.", max_length=64)]
ProfileQuery = Annotated[
    str | None,
    Query(description="Narrow to one profile; account-wide topics are always included."),
]
LimitQuery = Annotated[int, Query(ge=1, le=100, description="How many rows to return.")]


@router.get(
    "",
    operation_id="list_memory_topics",
    summary="The topic index: what is known about, one line each",
    response_model=TopicPage,
    responses=_VALIDATED,
    description=(
        "Every subject this person has memories about, most recently touched first, with a "
        "title, a one-line summary and a live count. This is the thing to carry in context "
        "every turn; call `get_memory_topic` when you need what is actually inside one.\n\n"
        "`total` is how many topics exist, not how many came back, so you can tell "
        '"showing 12 of 47" from "that is all of them". `unconfirmed` counts memories in '
        "the topic that came from a third party and have not been confirmed -- they are not "
        "in `memory_count` and never reach retrieval."
    ),
)
async def list_memory_topics(
    caller: CurrentCallerDep,
    store: StoreDep,
    profile: ProfileQuery = None,
    limit: LimitQuery = 50,
) -> TopicPage:
    """Return the topic index for the verified caller."""
    account = caller.account_id
    return await store.call(lambda handle: handle.topics(account, profile, limit))


@router.get(
    "/{topic_id}",
    operation_id="get_memory_topic",
    summary="Expand one topic into the memories behind it",
    response_model=TopicDetail,
    responses=_ADDRESSED,
    description=(
        "The topic plus its memories, most recently used first. Untrusted members are left "
        "out here too, so what comes back is safe to reason from.\n\n"
        "A topic whose memories have all been forgotten or superseded answers 404: it is a "
        "title with nothing behind it, and returning it would send you looking for "
        "something that is not there."
    ),
)
async def get_memory_topic(
    topic_id: TopicIdPath,
    caller: CurrentCallerDep,
    store: StoreDep,
    limit: LimitQuery = 50,
) -> TopicDetail:
    """Return one topic and the memories in it."""
    account = caller.account_id
    return await store.call(lambda handle: handle.topic(account, topic_id, limit))


@router.patch(
    "/{topic_id}",
    operation_id="update_memory_topic",
    summary="Give a topic a better title or summary",
    response_model=Topic,
    responses=_ADDRESSED,
    description=(
        "What a consolidation pass writes back after reading a topic: a title and a summary "
        "that describe the whole subject rather than whichever memory happened to create "
        "it.\n\n"
        "Membership is not editable from here, and that is the safeguard: a bad "
        "summarising pass can make the index read poorly, but it can never quietly move a "
        "fact into another subject. Send one field or both; sending neither is a 422."
    ),
)
async def update_memory_topic(
    topic_id: TopicIdPath, request: TopicUpdate, caller: CurrentCallerDep, store: StoreDep
) -> Topic:
    """Retitle or resummarise one topic."""
    account = caller.account_id
    return await store.call(lambda handle: handle.update_topic(account, topic_id, request))
