"""The background pass that collapses idle memories in one topic into a summary.

A topic that keeps collecting facts the person has not needed in a month is ranking noise:
retrieval surfaces the newest of them rather than the thing they all jointly say. Merging
them into one ``kind=summary`` row, and superseding the originals, is how the index stays
honest without throwing the history away.

The originals stay listable. Consolidation is not erasure.

## Why a pass that fails does not stop the next one

Same reason as the sweeper. A locked database for a minute would otherwise end ranking
hygiene for as long as the process lives. Cancellation is still ``BaseException`` and is
not swallowed: that is shutdown. Every pass logs ``consolidation_completed summaries=N`` or
``consolidation_failed error_type=...``, the type name only, as the sweeper does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    class Consolidatable(Protocol):
        """Somewhere to run one store call.

        Lives in the type-checking block so the coverage gate is not asked to execute a
        Protocol body nobody ever will.
        """

        async def call(self, operation: Callable[[Any], int]) -> int: ...


logger = logging.getLogger(__name__)


async def run_consolidator(
    store: Consolidatable,
    *,
    idle_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Merge idle topic members into a summary row, on a timer.

    It sleeps first. A process that restarts often would otherwise rewrite history on
    every boot, and a summary is cheaper to produce than to undo.
    """
    while True:
        await sleep(interval_seconds)
        try:
            written = await store.call(lambda connection: connection.consolidate(idle_seconds))
        except Exception as exc:
            logger.warning("consolidation_failed error_type=%s", type(exc).__name__)
        else:
            logger.info("consolidation_completed summaries=%d", written)


def start_consolidator(
    store: Consolidatable, *, idle_seconds: float, interval_seconds: float
) -> asyncio.Task[None] | None:
    """Begin consolidating, or nothing at all when an operator has turned it off."""
    if interval_seconds <= 0:
        return None
    return asyncio.create_task(
        run_consolidator(store, idle_seconds=idle_seconds, interval_seconds=interval_seconds),
        name="memory-consolidator",
    )


async def stop_consolidator(task: asyncio.Task[None] | None) -> None:
    """Stop it, and wait for it to notice, so shutdown does not leave a task running."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
