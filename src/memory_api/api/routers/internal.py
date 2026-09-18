"""The two-credential surface siblings use. Not an MCP tool surface.

A registered service must present its own token *and* the person's memory-api token. The
account still comes from the person's token, so a service cannot name who it is asking
about. ``operation_id``s are prefixed ``internal_`` so they never collide with the
person-facing names a model is given.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from memory_api.api.dependencies import SelectionDep, ServiceCallerDep, StoreDep, asserted_by
from memory_api.api.schemas import BlockList, Problem
from memory_api.domain.models import Memory, MemoryInput, Page, TopicDetail, TopicPage

router = APIRouter(prefix="/v1/internal/memory", tags=["internal"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_VALIDATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}

MemoryIdPath = Annotated[str, Path(description="The memory's id.", max_length=64)]
TopicIdPath = Annotated[str, Path(description="The topic's id.", max_length=64)]
ProfileQuery = Annotated[
    str | None,
    Query(description="Narrow to one profile; account-wide topics are always included."),
]
LimitQuery = Annotated[int, Query(ge=1, le=100, description="How many rows to return.")]

NOT_YOURS = (
    "A memory belonging to another account answers 404, the same as one that never "
    "existed. The account comes from the person's token, never from a parameter."
)
FOR_A_SIBLING = (
    "Takes two credentials: the calling service's token as Bearer, and the person's "
    "memory-api token in X-Keyring-User-Token. A model is never given these operations."
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="internal_create_memory",
    summary="Remember something on a person's behalf",
    response_model=Memory,
    responses=_VALIDATED,
    description="Records one memory for the person named by the user token. " + FOR_A_SIBLING,
)
async def create_memory(request: MemoryInput, caller: ServiceCallerDep, store: StoreDep) -> Memory:
    account, author = caller.account_id, asserted_by(caller)
    return await store.call(lambda handle: handle.write(account, author, request))


@router.get(
    "",
    operation_id="internal_list_memories",
    summary="List a person's memories with provenance",
    response_model=Page[Memory],
    responses=_VALIDATED,
    description="The audit view, including untrusted claims. " + FOR_A_SIBLING,
)
async def list_memories(
    selection: SelectionDep, caller: ServiceCallerDep, store: StoreDep
) -> Page[Memory]:
    account = caller.account_id
    return await store.call(lambda handle: handle.listing(account, selection, retrieval=False))


@router.get(
    "/search",
    operation_id="internal_search_memories",
    summary="Retrieve what is safe to put in front of the model",
    response_model=Page[Memory],
    responses=_ADDRESSED,
    description="The retrieval view: current, trusted, ranked. " + FOR_A_SIBLING,
)
async def search_memories(
    selection: SelectionDep, caller: ServiceCallerDep, store: StoreDep
) -> Page[Memory]:
    account = caller.account_id
    return await store.call(lambda handle: handle.listing(account, selection, retrieval=True))


@router.get(
    "/blocks",
    operation_id="internal_list_memory_blocks",
    summary="List the always-in-context blocks",
    response_model=BlockList,
    responses=_VALIDATED,
    description="The small labelled blocks carried every turn. " + FOR_A_SIBLING,
)
async def list_memory_blocks(caller: ServiceCallerDep, store: StoreDep) -> BlockList:
    account = caller.account_id
    return BlockList(data=await store.call(lambda handle: handle.blocks(account)))


@router.get(
    "/topics",
    operation_id="internal_list_memory_topics",
    summary="The topic index for this person",
    response_model=TopicPage,
    responses=_VALIDATED,
    description="One line per subject, most recently touched first. " + FOR_A_SIBLING,
)
async def list_memory_topics(
    caller: ServiceCallerDep,
    store: StoreDep,
    profile: ProfileQuery = None,
    limit: LimitQuery = 50,
) -> TopicPage:
    account = caller.account_id
    return await store.call(lambda handle: handle.topics(account, profile, limit))


@router.get(
    "/topics/{topic_id}",
    operation_id="internal_get_memory_topic",
    summary="Expand one topic into the memories behind it",
    response_model=TopicDetail,
    responses=_ADDRESSED,
    description="The topic plus its current trusted members. " + FOR_A_SIBLING,
)
async def get_memory_topic(
    topic_id: TopicIdPath,
    caller: ServiceCallerDep,
    store: StoreDep,
    limit: LimitQuery = 50,
) -> TopicDetail:
    account = caller.account_id
    return await store.call(lambda handle: handle.topic(account, topic_id, limit))


@router.get(
    "/{memory_id}",
    operation_id="internal_get_memory",
    summary="Read one memory",
    response_model=Memory,
    responses=_ADDRESSED,
    description="Returns one memory with its full provenance. " + NOT_YOURS,
)
async def get_memory(memory_id: MemoryIdPath, caller: ServiceCallerDep, store: StoreDep) -> Memory:
    account = caller.account_id
    return await store.call(lambda handle: handle.get(account, memory_id))


@router.post(
    "/{memory_id}/correct",
    status_code=status.HTTP_201_CREATED,
    operation_id="internal_correct_memory",
    summary="Replace a memory with a corrected one, keeping the history",
    response_model=Memory,
    responses={**_ADDRESSED, status.HTTP_409_CONFLICT: _PROBLEM},
    description="Writes a new memory and retires the old one, linked. " + FOR_A_SIBLING,
)
async def correct_memory(
    memory_id: MemoryIdPath, request: MemoryInput, caller: ServiceCallerDep, store: StoreDep
) -> Memory:
    account, author = caller.account_id, asserted_by(caller)
    return await store.call(lambda handle: handle.write(account, author, request, memory_id))


@router.post(
    "/{memory_id}/confirm",
    operation_id="internal_confirm_memory",
    summary="Confirm an untrusted memory so retrieval may use it",
    response_model=Memory,
    responses=_ADDRESSED,
    description="Promotes untrusted to stated. Confirm only what the person confirmed. "
    + FOR_A_SIBLING,
)
async def confirm_memory(
    memory_id: MemoryIdPath, caller: ServiceCallerDep, store: StoreDep
) -> Memory:
    account = caller.account_id
    return await store.call(lambda handle: handle.transition(account, memory_id, "confirm"))


@router.post(
    "/{memory_id}/forget",
    operation_id="internal_forget_memory",
    summary="Forget one memory",
    response_model=Memory,
    responses=_ADDRESSED,
    description="Hides the memory from both views and schedules it for erasure. " + FOR_A_SIBLING,
)
async def forget_memory(
    memory_id: MemoryIdPath, caller: ServiceCallerDep, store: StoreDep
) -> Memory:
    account = caller.account_id
    return await store.call(lambda handle: handle.transition(account, memory_id, "forget"))
