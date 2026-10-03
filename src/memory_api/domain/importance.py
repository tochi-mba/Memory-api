"""The importance floor: what is significant enough to be newly remembered.

Importance travels on every write, as :attr:`~memory_api.domain.models.MemoryInput.importance`
-- one to ten, five when the caller says nothing. A person may choose a floor below which
nothing new is written down about them (``memory.write_importance_floor`` in settings-api).
These rules decide what that floor applies to.

**New memories only.** A create, or an ``ADD`` in a reconciled batch, that would put a new
memory in the store. A correction -- ``correct_memory``, or a batch ``UPDATE`` -- replaces
something already remembered, and refusing it for being unimportant would leave the claim
it corrects standing as the truth. Nothing more is written down about the person by
letting it through: one current row goes in, one goes out of retrieval.

**A repeat is a revision, not a new memory.** Writing a claim already remembered stores
nothing new: the store folds the repeat's importance, expiry and confidence into the memory
it duplicates. Holding that to the floor would refuse exactly the write that downgrades or
retires something already known, and the floor would then keep *more* about the person,
not less. Only the store knows whether a write is a repeat, and only inside the transaction
that applies it, so it is the store that holds a write to the floor: see :func:`hold`.

**A refusal, never a silent drop.** A 201 for a memory that was not stored would leave the
caller believing something it is not, and a batch that skipped a decision would break
``data[i]`` being decision ``i``'s memory. A batch is all or nothing already, so one ``ADD``
below the floor refuses the whole batch and says which decisions were the reason.

**Read the floor only when it is needed.** :func:`within_floor` first applies a write with
the floor unasked, and asks only if the store finds a new memory in it. A repeat, a
correction and a batch that adds nothing new never ask -- settings-api is not even asked --
so settings-api being unreachable, slow or refusing this service does not fail or delay an
operation the floor has no say in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_api.domain.errors import BelowImportanceFloorError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from memory_api.domain.models import MemoryInput

LOWEST = 1
"""The least importance a memory can carry, so a floor here keeps everything.

What every person had before anybody could choose a floor, and what everybody still gets
when settings-api is not configured."""

HIGHEST = 10
"""The most importance a memory can carry. A floor above it would refuse every write."""

UNASKED = None
"""The floor a write is first applied with: not known yet, and asked for only if needed."""

BELOW_FLOOR = (
    "This memory is less important than this person chose to have remembered, so it was not "
    "stored. Raise its importance only if it really matters more."
)


class FloorNotAskedError(Exception):
    """A write would add a new memory, and the floor it is held to has not been asked for.

    Not a :class:`~memory_api.domain.errors.MemoryFault`: it never reaches a caller.
    :func:`within_floor` catches it, asks for the floor and applies the write again. Raised
    inside the store's transaction, it rolls back everything the attempt did.
    """


def hold(memory: MemoryInput, floor: int | None) -> None:
    """Hold one *new* memory to ``floor``; the store calls it for nothing else.

    Raises:
        FloorNotAskedError: ``floor`` is :data:`UNASKED`.
        BelowImportanceFloorError: the memory is less important than ``floor``.
    """
    if floor is None:  # UNASKED
        raise FloorNotAskedError
    if memory.importance < floor:
        raise BelowImportanceFloorError(BELOW_FLOOR)


def refuse_batch(below: Sequence[int]) -> BelowImportanceFloorError:
    """The refusal for a batch whose ``ADD`` decisions at ``below`` are under the floor.

    It names their positions, never their content or the floor.
    """
    message = (
        "Nothing in the batch was applied: these decisions add memories less important "
        f"than this person chose to have remembered: {', '.join(map(str, below))}. Leave "
        "them out and send the rest again."
    )
    return BelowImportanceFloorError(message)


async def within_floor[T](
    apply: Callable[[int | None], Awaitable[T]], floor: Callable[[], Awaitable[int]]
) -> T:
    """Apply a write, asking for the floor only if the write turns out to add a memory.

    ``apply`` runs the write in the store with the floor it is given. It is tried first with
    :data:`UNASKED`, which applies a repeat or a correction as it is and rolls back at the
    first new memory. Only then is ``floor`` awaited -- it is what asks settings-api -- and
    the write applied again, in full, held to it. A write that became new or became a
    repeat between the two attempts is judged by what the store holds at the second.

    Raises:
        BelowImportanceFloorError: from the store, for a new memory below the floor.
        PreferencesUnavailableError: from ``floor``, when it cannot be read honestly.
    """
    try:
        return await apply(UNASKED)
    except FloorNotAskedError:
        level = await floor()
    return await apply(level)
