"""The person-facing memory surface: fifteen operations under ``/v1/memory``.

Every ``operation_id`` here is public API. They become MCP tool names, so renaming one
breaks every client with a tool bound to it, and a contract test pins the exact set.

**Route order matters.** Within a router, Starlette matches in declaration order, so
``/v1/memory/search`` declared after ``/v1/memory/{memory_id}`` is never reached: it is
answered with "no memory called search". Every literal path is therefore declared first, and
a test reads the router's own route list rather than trusting a comment. (Across routers,
FastAPI 0.141 ranks a literal path ahead of another router's parameterised one, which is why
the topic routes survive in their own module -- but that is its behaviour, not a guarantee
worth depending on, so ``routers/__init__.py`` still includes them first.)

**No route accepts an account id.** Whose memories these are comes from the verified token.
That is the service's whole security property: a cross-account read is not refused by a
check, it is unexpressible, because no handler has anywhere to put another person's id.

**Two views of the same rows.** ``GET /v1/memory`` is the full view -- provenance, history,
untrusted claims, everything -- for a person auditing what is held about them. ``GET
/v1/memory/search`` is the retrieval view, which is what goes into a prompt: current only,
untrusted excluded, session episodes excluded from other sessions, ranked. They are separate
routes rather than a flag because the difference is a security boundary, and a boolean is
too easy to get wrong in a hurry.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response, status

from memory_api.api.dependencies import CurrentCallerDep, SelectionDep, StoreDep, asserted_by
from memory_api.api.schemas import BatchResult, BlockList, ForgetAllResult, Problem
from memory_api.domain.models import Batch, Block, BlockInput, Memory, MemoryInput, Page

router = APIRouter(prefix="/v1/memory", tags=["memory"])

_PROBLEM: dict[str, Any] = {"model": Problem}

# Declaring 422 explicitly is not decoration. FastAPI inserts its own `HTTPValidationError`
# response for any route with a parameter or a body unless 422 is already declared, and
# that document describes a body this service never sends: every failure here is a problem
# document. A published contract that is wrong about the error shape is worse than a silent
# one, because a client writes a parser against it.
_AUTH: dict[int | str, dict[str, Any]] = {status.HTTP_401_UNAUTHORIZED: _PROBLEM}
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
LabelPath = Annotated[
    str,
    Path(description="Which block, e.g. `persona`, `human`.", min_length=1, max_length=64),
]

NOT_YOURS = (
    "A memory belonging to another account answers 404, the same as one that never "
    "existed. Nothing here can name an account: whose memories these are comes from the "
    "token."
)


# -- Writes -----------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_memory",
    summary="Remember something",
    response_model=Memory,
    responses=_VALIDATED,
    description=(
        "Records one memory for the person whose token this is.\n\n"
        "`source` is your claim about where this came from; `asserted_by` is filled in from "
        "the verified token and cannot be set. Set `trust: untrusted` for anything a third "
        "party said -- web content, an email, a document -- and it will be stored but kept "
        "out of retrieval until somebody confirms it.\n\n"
        "Writing the same thing twice returns the memory that already exists rather than a "
        "duplicate. Credential-shaped content is refused: put secrets in keyring and "
        "remember a description of them here instead."
    ),
)
async def create_memory(request: MemoryInput, caller: CurrentCallerDep, store: StoreDep) -> Memory:
    """Write one memory for the verified caller."""
    account, author = caller.account_id, asserted_by(caller)
    return await store.call(lambda handle: handle.write(account, author, request))


# -- Reads: literal paths first, because Starlette matches in order ----------------------


@router.get(
    "",
    operation_id="list_memories",
    summary="List everything held, with its provenance",
    response_model=Page[Memory],
    responses=_ADDRESSED,
    description=(
        "The audit view: every memory, with where it came from, who asserted it, what it "
        "superseded and when it stops being true. Use `include_forgotten` and "
        "`include_history` to see what was withdrawn or replaced.\n\n"
        "This is what you show a person who asks what is remembered about them. It is not "
        "what you put in a prompt -- it deliberately includes untrusted claims. Use "
        "`search_memories` for that."
    ),
)
async def list_memories(
    selection: SelectionDep, caller: CurrentCallerDep, store: StoreDep
) -> Page[Memory]:
    """Page through everything stored for the verified caller."""
    account = caller.account_id
    return await store.call(lambda handle: handle.listing(account, selection, retrieval=False))


@router.get(
    "/search",
    operation_id="search_memories",
    summary="Retrieve what is worth putting in front of the model",
    response_model=Page[Memory],
    responses=_ADDRESSED,
    description=(
        "The retrieval view, ranked by relevance, recency and importance together. Pass `q` "
        "for keyword search; omit it to get the most relevant memories for the current "
        "profile and session.\n\n"
        "Four things are excluded and each exclusion is deliberate: untrusted memories, "
        "because their content came from somebody who is not this person; expired ones; "
        "superseded ones, so a correction wins over what it corrected; and episodes from "
        "other sessions, because a conversation's own turns are not facts about a life.\n\n"
        "`as_of` asks what was true at a past instant, which is how you answer a question "
        "about a time before a correction was made."
    ),
)
async def search_memories(
    selection: SelectionDep, caller: CurrentCallerDep, store: StoreDep
) -> Page[Memory]:
    """Rank and return the memories safe to reason from."""
    account = caller.account_id
    return await store.call(lambda handle: handle.listing(account, selection, retrieval=True))


@router.get(
    "/blocks",
    operation_id="list_memory_blocks",
    summary="List the always-in-context blocks",
    response_model=BlockList,
    responses=_AUTH,
    description=(
        "Blocks are the small, hand-maintained sections an assistant carries every turn -- "
        "who the person is, who the assistant is meant to be. They are not searched and not "
        "ranked; they are always there, which is why each one has a character limit."
    ),
)
async def list_memory_blocks(caller: CurrentCallerDep, store: StoreDep) -> BlockList:
    """Return every block this account has."""
    account = caller.account_id
    return BlockList(data=await store.call(lambda handle: handle.blocks(account)))


@router.get(
    "/blocks/{label}",
    operation_id="get_memory_block",
    summary="Read one block",
    response_model=Block,
    responses=_ADDRESSED,
    description="Returns the block's body and its character limit. " + NOT_YOURS,
)
async def get_memory_block(label: LabelPath, caller: CurrentCallerDep, store: StoreDep) -> Block:
    """Read one block by label."""
    account = caller.account_id
    return await store.call(lambda handle: handle.block(account, label))


@router.put(
    "/blocks/{label}",
    operation_id="write_memory_block",
    summary="Write one block",
    response_model=Block,
    responses=_VALIDATED,
    description=(
        "Replaces the block wholesale; there is no partial edit, because a block is small "
        "enough to rewrite and a patch language would be a second thing to get wrong.\n\n"
        "A body longer than `char_limit` is refused rather than truncated: silently cutting "
        "the end off a block loses whatever the person put at the bottom of it."
    ),
)
async def write_memory_block(
    label: LabelPath, request: BlockInput, caller: CurrentCallerDep, store: StoreDep
) -> Block:
    """Create or replace one block."""
    account = caller.account_id
    return await store.call(lambda handle: handle.block(account, label, request))


@router.delete(
    "/blocks/{label}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_memory_block",
    summary="Remove one block",
    responses=_VALIDATED,
    description=(
        "Deleting a block that is not there succeeds. A delete that answered 404 would make "
        "a retry after a dropped connection look like a failure."
    ),
)
async def delete_memory_block(
    label: LabelPath, caller: CurrentCallerDep, store: StoreDep
) -> Response:
    """Remove one block, whether or not it existed."""
    account = caller.account_id
    await store.call(lambda handle: handle.delete_block(account, label))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/batch",
    operation_id="reconcile_memories",
    summary="Apply a reconciled set of decisions in one transaction",
    response_model=BatchResult,
    responses={**_ADDRESSED, status.HTTP_409_CONFLICT: _PROBLEM},
    description=(
        "Takes the output of an extraction pass -- ADD, UPDATE, DELETE, NOOP -- and applies "
        "it atomically. Either every decision lands or none does, so a failure halfway "
        "through cannot leave a correction stored without the thing it corrected being "
        "retired.\n\n"
        "Send NOOP for what you decided to leave alone. It costs one row read and makes the "
        "result a complete account of what the pass considered, rather than only of what it "
        "changed. `data[i]` is the memory decision `i` produced, in order."
    ),
)
async def reconcile_memories(
    request: Batch, caller: CurrentCallerDep, store: StoreDep
) -> BatchResult:
    """Apply every decision in one transaction."""
    account, author = caller.account_id, asserted_by(caller)
    applied = await store.call(lambda handle: handle.batch(account, author, request.decisions))
    return BatchResult(data=applied)


@router.delete(
    "",
    operation_id="forget_all_memories",
    summary="Forget everything held for this account",
    response_model=ForgetAllResult,
    responses=_AUTH,
    description=(
        "Marks every memory forgotten and removes every block. Forgotten memories are "
        "invisible to both views immediately and are erased for good once the grace period "
        "passes, which is what makes an accidental call recoverable for as long as somebody "
        "could plausibly notice.\n\n"
        "Returns how many memories were still remembered. It does not return them: handing "
        "back what was just erased would be a copy of it."
    ),
)
async def forget_all_memories(caller: CurrentCallerDep, store: StoreDep) -> ForgetAllResult:
    """Forget every memory and drop every block for this account."""
    account = caller.account_id
    return ForgetAllResult(forgotten=await store.call(lambda handle: handle.forget_all(account)))


# -- One memory: parameterised paths, declared last --------------------------------------


@router.get(
    "/{memory_id}",
    operation_id="get_memory",
    summary="Read one memory",
    response_model=Memory,
    responses=_ADDRESSED,
    description="Returns one memory with its full provenance. " + NOT_YOURS,
)
async def get_memory(memory_id: MemoryIdPath, caller: CurrentCallerDep, store: StoreDep) -> Memory:
    """Read one memory by id."""
    account = caller.account_id
    return await store.call(lambda handle: handle.get(account, memory_id))


@router.post(
    "/{memory_id}/correct",
    status_code=status.HTTP_201_CREATED,
    operation_id="correct_memory",
    summary="Replace a memory with a corrected one, keeping the history",
    response_model=Memory,
    responses={**_ADDRESSED, status.HTTP_409_CONFLICT: _PROBLEM},
    description=(
        "Writes a new memory and retires the old one, linked. Use this whenever something "
        "changes -- never delete-then-add, which throws away the fact that it used to be "
        "true and breaks `as_of`.\n\n"
        "`valid_from` says when the new version started being true, and defaults to now. "
        "The correction inherits the topic of what it replaces, so a fact never gets "
        "separated from its own history. A correction must keep the original's scope, and "
        "cannot start before the memory it replaces."
    ),
)
async def correct_memory(
    memory_id: MemoryIdPath, request: MemoryInput, caller: CurrentCallerDep, store: StoreDep
) -> Memory:
    """Supersede one memory with a corrected version."""
    account, author = caller.account_id, asserted_by(caller)
    return await store.call(lambda handle: handle.write(account, author, request, memory_id))


@router.post(
    "/{memory_id}/confirm",
    operation_id="confirm_memory",
    summary="Confirm an untrusted memory so retrieval may use it",
    response_model=Memory,
    responses=_ADDRESSED,
    description=(
        "Promotes a memory from `untrusted` to `stated` and records when. Until this "
        "happens the memory is stored and listed but never retrieved, because its content "
        "came from somebody other than this person and retrieval is what reaches a prompt.\n"
        "\n"
        "Confirm only what the person themselves confirmed. Confirming on their behalf "
        "defeats the whole point of the trust level."
    ),
)
async def confirm_memory(
    memory_id: MemoryIdPath, caller: CurrentCallerDep, store: StoreDep
) -> Memory:
    """Mark one memory as confirmed by the person."""
    account = caller.account_id
    return await store.call(lambda handle: handle.transition(account, memory_id, "confirm"))


@router.post(
    "/{memory_id}/forget",
    operation_id="forget_memory",
    summary="Forget one memory",
    response_model=Memory,
    responses=_ADDRESSED,
    description=(
        "Hides the memory from both views at once and schedules it for erasure. Reversible "
        "with `restore_memory` until the grace period runs out, which is the difference "
        "between a person changing their mind and a person losing something.\n\n"
        "Forgetting is already applied returns the memory unchanged rather than failing."
    ),
)
async def forget_memory(
    memory_id: MemoryIdPath, caller: CurrentCallerDep, store: StoreDep
) -> Memory:
    """Mark one memory forgotten."""
    account = caller.account_id
    return await store.call(lambda handle: handle.transition(account, memory_id, "forget"))


@router.post(
    "/{memory_id}/restore",
    operation_id="restore_memory",
    summary="Restore a forgotten memory",
    response_model=Memory,
    responses=_ADDRESSED,
    description=(
        "Undoes a forget, provided the grace period has not yet erased the row. Restoring "
        "something that was never forgotten succeeds and changes nothing visible."
    ),
)
async def restore_memory(
    memory_id: MemoryIdPath, caller: CurrentCallerDep, store: StoreDep
) -> Memory:
    """Bring one forgotten memory back."""
    account = caller.account_id
    return await store.call(lambda handle: handle.transition(account, memory_id, "restore"))


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_memory",
    summary="Forget one memory, for callers that expect DELETE",
    responses=_ADDRESSED,
    description=(
        "The same operation as `forget_memory`, spelled the way a REST client expects. It "
        "forgets rather than erases, for the same reason: a delete somebody regrets ten "
        "seconds later should be recoverable."
    ),
)
async def delete_memory(
    memory_id: MemoryIdPath, caller: CurrentCallerDep, store: StoreDep
) -> Response:
    """Forget one memory and answer with no content."""
    account = caller.account_id
    await store.call(lambda handle: handle.transition(account, memory_id, "forget"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
