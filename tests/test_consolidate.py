"""Idle memories in one topic become a single summary row.

Consolidation is ranking hygiene, not deletion: the originals are superseded so retrieval
sees the summary, and they remain listable in the audit view.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from conftest import ACCOUNT
from memory_api.core.consolidator import run_consolidator, start_consolidator, stop_consolidator
from memory_api.domain.models import MemoryInput, Selection, Trust
from memory_api.store.sql import SQLStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import FakeClock

DAY = 86_400.0


def _write(store: SQLStore, body: str) -> None:
    store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Favourite tea", body=body))


def test_two_idle_facts_in_one_topic_become_a_summary(store: SQLStore, clock: FakeClock) -> None:
    _write(store, "Earl Grey")
    _write(store, "Assam in the morning")
    clock.advance(40 * DAY)

    written = store.consolidate(30 * DAY)

    assert written == 1
    retrieved = store.listing(ACCOUNT, Selection(), retrieval=True)
    assert [row.kind for row in retrieved.data] == ["summary"]
    assert "Earl Grey" in retrieved.data[0].body
    assert retrieved.data[0].source == "consolidation"
    audit = store.listing(ACCOUNT, Selection(include_history=True), retrieval=False)
    assert len(audit.data) == 3


def test_a_merge_of_stated_facts_is_still_stated(store: SQLStore, clock: FakeClock) -> None:
    """The bug, named: every merge was stamped `inferred`, so a person's own stated facts fell
    out of each stated-only search once they had gone a month unused."""
    _write(store, "Earl Grey")
    _write(store, "Assam in the morning")
    clock.advance(40 * DAY)

    store.consolidate(30 * DAY)

    stated = store.listing(ACCOUNT, Selection(include_inferred=False), retrieval=True)
    assert [(row.kind, row.trust) for row in stated.data] == [("summary", "stated")]


@pytest.mark.parametrize(
    ("trusts", "merged"),
    [
        (("stated", "observed"), "observed"),
        (("observed", "inferred"), "inferred"),
        (("inferred", "stated"), "inferred"),
    ],
)
def test_a_merge_is_as_trusted_as_its_least_trusted_member(
    store: SQLStore, clock: FakeClock, trusts: tuple[Trust, Trust], merged: Trust
) -> None:
    for body, trust in zip(("Earl Grey", "Assam"), trusts, strict=True):
        store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Favourite tea", body=body, trust=trust))
    clock.advance(40 * DAY)

    store.consolidate(30 * DAY)

    retrieved = store.listing(ACCOUNT, Selection(), retrieval=True)
    assert [row.trust for row in retrieved.data] == [merged]


def test_a_lone_idle_memory_is_left_alone(store: SQLStore, clock: FakeClock) -> None:
    _write(store, "Earl Grey")
    clock.advance(40 * DAY)

    assert store.consolidate(30 * DAY) == 0
    retrieved = store.listing(ACCOUNT, Selection(), retrieval=True)
    assert [row.kind for row in retrieved.data] == ["fact"]


def test_a_recently_used_pair_is_not_merged(store: SQLStore, clock: FakeClock) -> None:
    _write(store, "Earl Grey")
    _write(store, "Assam in the morning")
    clock.advance(10 * DAY)

    assert store.consolidate(30 * DAY) == 0


def test_untrusted_idle_memories_are_not_merged(store: SQLStore, clock: FakeClock) -> None:
    store.write(
        ACCOUNT,
        ACCOUNT,
        MemoryInput(title="Favourite tea", body="From a page", trust="untrusted"),
    )
    store.write(
        ACCOUNT,
        ACCOUNT,
        MemoryInput(title="Favourite tea", body="Also from a page", trust="untrusted"),
    )
    clock.advance(40 * DAY)

    assert store.consolidate(30 * DAY) == 0


def test_a_zero_idle_window_does_not_rewrite_everything(store: SQLStore) -> None:
    _write(store, "Earl Grey")
    _write(store, "Assam")
    assert store.consolidate(0) == 0


class Recorder:
    """A store worker that remembers what it was asked to do."""

    def __init__(self, fail_times: int = 0) -> None:
        self.idle: list[float] = []
        self.fail_times = fail_times

    async def call(self, operation: Callable[[Recorder], int]) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            message = "database is locked"
            raise RuntimeError(message)
        return operation(self)

    def consolidate(self, idle_seconds: float) -> int:
        self.idle.append(idle_seconds)
        return 1


class Ticks:
    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        if len(self.slept) >= self.allowed:
            raise asyncio.CancelledError
        self.slept.append(seconds)


async def test_a_failed_pass_does_not_stop_the_next_one() -> None:
    store, sleep = Recorder(fail_times=2), Ticks(allowed=4)

    with pytest.raises(asyncio.CancelledError):
        await run_consolidator(store, idle_seconds=600.0, interval_seconds=60.0, sleep=sleep)

    assert store.idle == [600.0, 600.0]


async def test_an_interval_of_zero_is_an_operator_turning_it_off() -> None:
    assert start_consolidator(Recorder(), idle_seconds=1.0, interval_seconds=0) is None
    assert start_consolidator(Recorder(), idle_seconds=1.0, interval_seconds=-1) is None


async def test_stopping_something_that_was_never_started_is_not_an_error() -> None:
    await stop_consolidator(None)


async def test_a_started_consolidator_stops_when_it_is_asked_to() -> None:
    store = Recorder()
    task = start_consolidator(store, idle_seconds=600.0, interval_seconds=0.01)
    assert task is not None
    await asyncio.sleep(0.05)
    await stop_consolidator(task)
    assert task.done()
    assert store.idle


def test_a_row_without_a_topic_is_skipped(store: SQLStore, clock: FakeClock) -> None:
    _write(store, "Earl Grey")
    _write(store, "Assam")
    store.db.execute("UPDATE memories SET topic_id=NULL")
    store.db.commit()
    clock.advance(40 * DAY)
    assert store.consolidate(30 * DAY) == 0


def test_a_merge_that_looks_like_a_secret_is_left_alone(
    store: SQLStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memory_api.domain.errors import SecretError
    from memory_api.store import sql as sql_mod

    def boom(_value: object) -> None:
        message = "no"
        raise SecretError(message)

    _write(store, "Earl Grey")
    _write(store, "Assam in the morning")
    clock.advance(40 * DAY)
    monkeypatch.setattr(sql_mod, "refuse_secrets", boom)
    assert store.consolidate(30 * DAY) == 0


def test_a_summary_stays_on_the_members_topic(store: SQLStore, clock: FakeClock) -> None:
    _write(store, "Earl Grey")
    _write(store, "Assam in the morning")
    original = store.listing(ACCOUNT, Selection(), retrieval=False).data[0].topic_id
    store.db.execute(
        "UPDATE memories SET title=? WHERE body=?",
        ("A wholly different subject", "Earl Grey"),
    )
    store.db.commit()
    clock.advance(40 * DAY)
    assert store.consolidate(30 * DAY) == 1
    summary = store.listing(ACCOUNT, Selection(), retrieval=True).data[0]
    assert summary.topic_id == original


def _session_fact(store: SQLStore, session_id: str, body: str) -> None:
    store.write(
        ACCOUNT,
        ACCOUNT,
        MemoryInput(
            title="Favourite tea", body=body, scope="session", profile="home", session_id=session_id
        ),
    )


def test_a_session_memory_is_never_merged_into_another_compartment(
    store: SQLStore, clock: FakeClock
) -> None:
    # One topic, three compartments: the profile, and two sessions inside it. Merging across
    # them would hand the summary the oldest member's scope, so a session's facts would
    # surface in every session of the profile, or vanish into a session they never belonged
    # to -- the move a correction is refused for.
    store.write(
        ACCOUNT,
        ACCOUNT,
        MemoryInput(title="Favourite tea", body="Earl Grey", scope="profile", profile="home"),
    )
    _session_fact(store, "monday", "Assam in the morning")
    clock.advance(1)
    _session_fact(store, "monday", "Lapsang after dinner")
    _session_fact(store, "tuesday", "Rooibos on Tuesdays")
    clock.advance(40 * DAY)

    assert store.consolidate(30 * DAY) == 1

    def bodies(session_id: str) -> list[str]:
        selection = Selection(profile="home", session_id=session_id)
        return sorted(row.body for row in store.listing(ACCOUNT, selection, retrieval=True).data)

    assert bodies("monday") == ["Assam in the morning; Lapsang after dinner", "Earl Grey"]
    assert bodies("tuesday") == ["Earl Grey", "Rooibos on Tuesdays"]
    assert bodies("wednesday") == ["Earl Grey"]


def test_a_summary_never_leaves_a_topic_with_nothing_in_it(
    store: SQLStore, clock: FakeClock
) -> None:
    # A correction inherits its topic whatever it is titled, so the oldest current member
    # can carry a title that matches no topic. Giving the summary that title used to create
    # a topic for it and then move the summary back to the members' topic, leaving a row
    # that nothing pointed at.
    original = store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Home city", body="London"))
    clock.advance(1)
    store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Where I live", body="Bristol"), original.id)
    clock.advance(1)
    store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Home city", body="Near the harbour"))
    clock.advance(40 * DAY)

    assert store.consolidate(30 * DAY) == 1

    empty = store.db.execute(
        "SELECT COUNT(*) FROM topics t WHERE NOT EXISTS "
        "(SELECT 1 FROM memories m WHERE m.topic_id=t.id)"
    ).fetchone()[0]
    assert empty == 0
    summary = store.listing(ACCOUNT, Selection(), retrieval=True).data[0]
    assert summary.topic_id == original.topic_id


def test_a_summary_is_never_deduplicated_into_a_member_it_supersedes(
    store: SQLStore, clock: FakeClock
) -> None:
    # An earlier summary plus a fact with no body would produce a summary identical to the
    # earlier one. Treating that as a repeated write returned the earlier summary, which the
    # merge then marked superseded by itself -- and the topic had nothing current left.
    store.write(
        ACCOUNT,
        ACCOUNT,
        MemoryInput(title="Favourite tea", body="Earl Grey", kind="summary", trust="inferred"),
    )
    _write(store, "")
    clock.advance(40 * DAY)

    assert store.consolidate(30 * DAY) == 1

    retrieved = store.listing(ACCOUNT, Selection(), retrieval=True).data
    assert [(row.kind, row.body) for row in retrieved] == [("summary", "Earl Grey")]
    assert retrieved[0].superseded_by_id is None
    assert retrieved[0].source == "consolidation"


async def test_every_pass_says_what_it_did_and_a_failure_says_only_its_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, sleep = Recorder(fail_times=1), Ticks(allowed=2)
    caplog.set_level(logging.INFO, logger="memory_api.core.consolidator")

    with pytest.raises(asyncio.CancelledError):
        await run_consolidator(store, idle_seconds=600.0, interval_seconds=60.0, sleep=sleep)

    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("WARNING", "consolidation_failed error_type=RuntimeError"),
        ("INFO", "consolidation_completed summaries=1"),
    ]
    assert "locked" not in caplog.text
