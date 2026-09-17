"""The thread the database lives on.

Two properties are being pinned, and only one of them is about correctness of results.

The first is that every call really does land on the worker's thread. That is not checked
by inspecting anything: ``sqlite3`` is left with ``check_same_thread=True``, so a call from
the wrong thread raises rather than corrupting a transaction under load. The test asserts
the guard is still armed by deliberately tripping it, which is the only way to know the
discipline is being enforced rather than merely intended.

The second is that the event loop is never blocked. A synchronous store called from a route
would stall every other request for the length of a transaction, including ``/healthy`` --
so a slow request becomes a process that looks dead.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from threading import current_thread

import pytest

from memory_api.domain.models import MemoryInput, Selection
from memory_api.store.worker import StoreWorker

ACCOUNT = "acct_example"


class TestTheWorkerOwnsItsConnection:
    async def test_it_opens_the_database_on_its_own_thread_and_answers_from_there(
        self,
    ) -> None:
        worker = StoreWorker(":memory:")
        try:
            calling = current_thread().name
            running = await worker.call(lambda _: current_thread().name)
            assert running != calling
            assert running.startswith("memory-sqlite")
        finally:
            await worker.aclose()

    async def test_every_call_lands_on_the_same_thread(self) -> None:
        # One thread, not a pool of them: it serialises transactions, which is what SQLite
        # wants, and it is what makes the same-thread guard meaningful.
        worker = StoreWorker(":memory:")
        try:
            threads = {await worker.call(lambda _: current_thread().name) for _ in range(5)}
            assert len(threads) == 1
        finally:
            await worker.aclose()

    async def test_the_same_thread_guard_is_still_armed(self) -> None:
        # Reaching the connection from outside the worker must fail loudly. If this ever
        # starts passing, `check_same_thread` has been turned off and the whole discipline
        # has become unfalsifiable.
        worker = StoreWorker(":memory:")
        try:
            handle = await worker.call(lambda store: store)
            with pytest.raises(sqlite3.ProgrammingError, match="same thread"):
                handle.db.execute("SELECT 1")
        finally:
            await worker.aclose()

    async def test_a_call_does_not_block_the_event_loop(self) -> None:
        # The property the worker exists for, stated as a race: a slow store call and a
        # loop-bound task started together, with the loop-bound one finishing first.
        worker = StoreWorker(":memory:")
        order: list[str] = []

        async def tick() -> None:
            await asyncio.sleep(0)
            order.append("loop")

        def slow(_: object) -> str:
            time.sleep(0.05)
            order.append("store")
            return "done"

        try:
            await asyncio.gather(worker.call(slow), tick())
            assert order == ["loop", "store"]
        finally:
            await worker.aclose()


class TestWhereTheDatabaseFileGoes:
    async def test_the_directory_it_was_asked_for_is_created(self, tmp_path: Path) -> None:
        # A deployment configures the file it wants, not a directory it must remember to
        # create; otherwise the first boot fails with "unable to open database file".
        path = tmp_path / "var" / "nested" / "memory.db"
        worker = StoreWorker(str(path))
        try:
            await worker.call(
                lambda store: store.write(ACCOUNT, ACCOUNT, MemoryInput(title="Home city"))
            )
        finally:
            await worker.aclose()
        assert path.exists()

    async def test_what_was_written_survives_reopening_the_same_file(self, tmp_path: Path) -> None:
        path = str(tmp_path / "memory.db")
        first = StoreWorker(path)
        try:
            await first.call(
                lambda store: store.write(
                    ACCOUNT, ACCOUNT, MemoryInput(title="Home city", body="London")
                )
            )
        finally:
            await first.aclose()

        second = StoreWorker(path)
        try:
            page = await second.call(lambda store: store.listing(ACCOUNT, Selection()))
        finally:
            await second.aclose()
        assert [row.body for row in page.data] == ["London"]

    async def test_an_in_memory_database_creates_no_file_called_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `:memory:` is a sqlite sentinel, not a path. Treating it as one would put a file
        # literally called ":memory:" in the working directory and quietly make every test
        # share a database.
        monkeypatch.chdir(tmp_path)
        worker = StoreWorker(":memory:")
        try:
            await worker.call(lambda store: store.listing(ACCOUNT, Selection()))
        finally:
            await worker.aclose()
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


class TestReadiness:
    async def test_a_working_database_reports_healthy_with_no_reason(self) -> None:
        worker = StoreWorker(":memory:")
        try:
            assert await worker.healthy() == (True, None)
        finally:
            await worker.aclose()

    async def test_a_broken_database_reports_the_failures_type_and_not_its_message(
        self,
    ) -> None:
        # The message carries the path of the database file, and `/ready` is answered to
        # anybody who asks.
        worker = StoreWorker(":memory:")
        try:
            await worker.call(lambda store: store.close())
            usable, reason = await worker.healthy()
            assert usable is False
            assert reason == "ProgrammingError"
        finally:
            await worker.aclose()

    async def test_closing_twice_is_not_an_error(self) -> None:
        # `aclose` runs from a lifespan `finally`, so it has to survive a store that
        # already failed or was already closed.
        worker = StoreWorker(":memory:")
        await worker.aclose()
