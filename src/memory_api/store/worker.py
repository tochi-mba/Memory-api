"""Every SQLite call on one dedicated thread, including the connection's construction.

:class:`~memory_api.store.sql.SQLStore` is synchronous. Calling it from a route would block
the event loop for the duration of a transaction, and under concurrency that is not a
slow request but a stalled process: nothing else is served, including ``/healthy``.

So the store lives behind a pool of exactly one thread and is reached only through
:meth:`StoreWorker.call`. One thread rather than several is deliberate twice over. It
serialises transactions, which is what SQLite wants anyway -- a second writer gets
``SQLITE_BUSY`` and the retry logic to go with it. And it means the connection is only ever
touched by the thread that opened it.

That second property is *enforced*, not merely intended. ``sqlite3.connect`` defaults to
``check_same_thread=True`` and the store leaves it alone, so a call that escapes this worker
raises ``ProgrammingError`` at once instead of quietly corrupting a transaction under load.
Turning it off would make the discipline unfalsifiable, which is why nothing here does.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from memory_api.store.sql import SQLStore

if TYPE_CHECKING:
    from collections.abc import Callable

IN_MEMORY = ":memory:"


def _open(path: str, clock: Callable[[], float]) -> SQLStore:
    """Open the store, creating the directory the file is meant to live in.

    A deployment configures ``MEMORY_DATABASE_PATH`` as the file it wants, not as a
    directory it must remember to create first; without this the first boot after a fresh
    install fails with an ``unable to open database file`` that says nothing about why.
    """
    if path != IN_MEMORY:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SQLStore(path, clock)


class StoreWorker:
    """The only way into the store, and the only thread that ever touches it."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-sqlite")
        # Submitted rather than constructed here: the connection must be created on the
        # worker thread, because that is the thread every later call will arrive on.
        self._store = self._pool.submit(_open, path, clock)

    async def call[T](self, operation: Callable[[SQLStore], T]) -> T:
        """Run ``operation`` against the store, awaiting it without blocking the loop."""

        def execute() -> T:
            return operation(self._store.result())

        return await asyncio.wrap_future(self._pool.submit(execute))

    async def healthy(self) -> tuple[bool, str | None]:
        """Whether the database answers, and the kind of failure when it does not.

        The reason is the exception's *type name* and never its message: a sqlite error
        routinely carries the database path, and ``/ready`` is unauthenticated.
        """
        try:
            await self.call(lambda store: store.ping())
        # Deliberately broad: a probe that only caught the failures somebody thought of
        # would report a healthy database while the process could not read a row.
        except Exception as exc:
            return False, type(exc).__name__
        return True, None

    async def aclose(self) -> None:
        """Close the connection on its own thread, then retire the thread."""
        await self.call(lambda store: store.close())
        await asyncio.to_thread(self._pool.shutdown, wait=True)
