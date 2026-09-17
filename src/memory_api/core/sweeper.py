"""The thing that actually erases.

Forgetting and erasing are different acts, deliberately. A forget is reversible: it hides a
memory, keeps its history, and can be undone by somebody who did not mean it. Erasure is
not, which is why it waits.

But a grace period only means anything if something comes along afterwards. `SQLStore.sweep`
existed and had no caller anywhere in the service: no route, no schedule, no setting naming
the grace period. Every route description told people their memories would be "erased for
good once the grace period passes", and nothing would ever have erased them. A promise about
deletion that is not kept is not a missing feature, it is a false statement about what the
service does with somebody's data.

So this runs it. On a timer, in the background, for the life of the process.

## Why a pass that fails does not stop the next one

A locked database, a disk that is full for a minute, a WAL checkpoint that cannot complete:
every one of those is temporary, and every one of them would otherwise end erasure for as
long as the process lives. The loop survives them. What it cannot do yet is *say* that one
happened -- this service has no logging module, so a failed pass is currently silent, and
that gap is worth closing before anybody depends on the timing of an erasure.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    class Sweepable(Protocol):
        """Somewhere to run one store call.

        Narrower than `StoreWorker` on purpose. The sweeper needs a place to run work, not
        the whole store, and saying so is what lets a test drive the loop without a
        database, a thread and a file on disk.

        It lives in the type-checking block because it is only ever an annotation: a
        Protocol that exists at runtime is a class body the coverage gate expects somebody
        to execute, and nobody ever will.
        """

        async def call(self, operation: Callable[[Any], int]) -> int: ...


DAY_SECONDS = 86_400


async def run_sweeper(
    store: Sweepable,
    *,
    grace_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Erase what has been forgotten for longer than the grace period, on a timer.

    It sleeps first. A process that restarts often would otherwise sweep on every boot, and
    a sweep is the one operation here that cannot be undone -- the cheapest way to make a
    crash loop destructive is to put irreversible work at startup.

    `sleep` is injected so a test can drive the loop without waiting an hour for it.
    """
    while True:
        await sleep(interval_seconds)
        with contextlib.suppress(Exception):
            # Never `BaseException`: a cancellation is the process shutting down, and
            # swallowing it here would keep the loop alive past the end of the application.
            await store.call(lambda connection: connection.sweep(grace_seconds))


def start_sweeper(
    store: Sweepable, *, grace_seconds: float, interval_seconds: float
) -> asyncio.Task[None] | None:
    """Begin sweeping, or nothing at all when an operator has turned it off.

    An interval of zero disables it. That is an operator's decision to make -- some
    deployments erase on their own schedule, and one that runs two processes against one
    database wants exactly one of them doing this -- but it is a decision, not a default.
    """
    if interval_seconds <= 0:
        return None
    return asyncio.create_task(
        run_sweeper(store, grace_seconds=grace_seconds, interval_seconds=interval_seconds),
        name="memory-sweeper",
    )


async def stop_sweeper(task: asyncio.Task[None] | None) -> None:
    """Stop it, and wait for it to notice, so shutdown does not leave a task running."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
