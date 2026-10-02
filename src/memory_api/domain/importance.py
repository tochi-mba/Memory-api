"""The importance floor: what is significant enough to be newly remembered.

Importance travels on every write, as :attr:`~memory_api.domain.models.MemoryInput.importance`
-- one to ten, five when the caller says nothing. A person may choose a floor below which
nothing new is written down about them (``memory.write_importance_floor`` in settings-api).
These rules decide what that floor applies to.

**New memories only.** A create, and an ``ADD`` in a reconciled batch. A correction --
``correct_memory``, or a batch ``UPDATE`` -- replaces something already remembered, and
refusing it for being unimportant would leave the claim it corrects standing as the truth.
Nothing more is written down about the person by letting it through: one current row goes
in, one goes out of retrieval.

**A refusal, never a silent drop.** A 201 for a memory that was not stored would leave the
caller believing something it is not, and a batch that skipped a decision would break
``data[i]`` being decision ``i``'s memory. A batch is all or nothing already, so one ``ADD``
below the floor refuses the whole batch and says which decisions were the reason.

**Read the floor only when it is needed.** A batch of corrections and forgets never asks for
it, so settings-api being unreachable does not fail an operation the floor has no say in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_api.domain.errors import BelowImportanceFloorError
from memory_api.domain.models import MemoryInput

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from memory_api.domain.models import Decision

LOWEST = 1
"""The least importance a memory can carry, so a floor here keeps everything.

What every person had before anybody could choose a floor, and what everybody still gets
when settings-api is not configured."""

HIGHEST = 10
"""The most importance a memory can carry. A floor above it would refuse every write."""

BELOW_FLOOR = (
    "This memory is less important than this person chose to have remembered, so it was not "
    "stored. Raise its importance only if it really matters more."
)


def refuse_below_floor(memory: MemoryInput, floor: int) -> None:
    """Refuse one new memory whose importance is under ``floor``.

    Raises:
        BelowImportanceFloorError: it is.
    """
    if memory.importance < floor:
        raise BelowImportanceFloorError(BELOW_FLOOR)


def refuse_additions_below_floor(decisions: Sequence[Decision], floor: Callable[[], int]) -> None:
    """Refuse a batch if any ``ADD`` in it is under the floor; nothing else is held to it.

    ``floor`` is called at most once, and only when the batch adds something.

    Raises:
        BelowImportanceFloorError: naming the positions of the decisions below it, never
            their content or the floor.
        PreferencesUnavailableError: from ``floor``, when it cannot be read honestly.
    """
    additions = [
        (index, MemoryInput.model_validate(decision.memory))
        for index, decision in enumerate(decisions)
        if decision.action == "ADD"
    ]
    if not additions:
        return
    level = floor()
    below = [str(index) for index, memory in additions if memory.importance < level]
    if below:
        message = (
            "Nothing in the batch was applied: these decisions add memories less important "
            f"than this person chose to have remembered: {', '.join(below)}. Leave them out "
            "and send the rest again."
        )
        raise BelowImportanceFloorError(message)
