"""The background job that turns a forget into an erasure.

What is pinned here is mostly that it keeps going. A sweeper that stops on the first locked
database is indistinguishable, from the outside, from the one this service shipped with
first: the one that never ran at all.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from memory_api.core.sweeper import run_sweeper, start_sweeper, stop_sweeper

if TYPE_CHECKING:
    from collections.abc import Callable


class Recorder:
    """A store worker that remembers what it was asked to do."""

    def __init__(self, fail_times: int = 0) -> None:
        self.grace: list[float] = []
        self.fail_times = fail_times

    async def call(self, operation: Callable[[Recorder], int]) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            message = "database is locked"
            raise RuntimeError(message)
        return operation(self)

    def sweep(self, grace_seconds: float) -> int:
        self.grace.append(grace_seconds)
        return 1


class Ticks:
    """A clock the test drives, which stops the loop by refusing to tick again."""

    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        if len(self.slept) >= self.allowed:
            raise asyncio.CancelledError
        self.slept.append(seconds)


async def test_it_sweeps_on_the_interval_with_the_configured_grace() -> None:
    store, sleep = Recorder(), Ticks(allowed=3)

    with pytest.raises(asyncio.CancelledError):
        await run_sweeper(store, grace_seconds=600.0, interval_seconds=60.0, sleep=sleep)

    assert sleep.slept == [60.0, 60.0, 60.0]
    assert store.grace == [600.0, 600.0, 600.0]


async def test_it_waits_before_the_first_sweep_rather_than_erasing_at_startup() -> None:
    """A crash loop must not be a way to destroy data faster.

    Sweeping is the one irreversible thing this service does, so it never happens as a
    consequence of a process starting.
    """
    store, sleep = Recorder(), Ticks(allowed=0)

    with pytest.raises(asyncio.CancelledError):
        await run_sweeper(store, grace_seconds=600.0, interval_seconds=60.0, sleep=sleep)

    assert store.grace == [], "nothing was erased before the first interval elapsed"


async def test_a_pass_that_fails_does_not_end_erasure_for_the_life_of_the_process() -> None:
    store, sleep = Recorder(fail_times=2), Ticks(allowed=4)

    with pytest.raises(asyncio.CancelledError):
        await run_sweeper(store, grace_seconds=600.0, interval_seconds=60.0, sleep=sleep)

    assert store.grace == [600.0, 600.0], "the two passes after the failures still ran"


async def test_an_interval_of_zero_is_an_operator_turning_it_off() -> None:
    assert start_sweeper(Recorder(), grace_seconds=1.0, interval_seconds=0) is None
    assert start_sweeper(Recorder(), grace_seconds=1.0, interval_seconds=-1) is None


async def test_stopping_something_that_was_never_started_is_not_an_error() -> None:
    await stop_sweeper(None)


async def test_a_started_sweeper_stops_when_it_is_asked_to() -> None:
    store = Recorder()
    task = start_sweeper(store, grace_seconds=600.0, interval_seconds=0.01)
    assert task is not None

    await asyncio.sleep(0.05)
    await stop_sweeper(task)

    assert task.done()
    assert store.grace, "it did sweep at least once before being stopped"
