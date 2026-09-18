"""One SQLite connection, used exclusively by the repository's worker thread."""

from __future__ import annotations

import contextlib
import json
import math
import secrets
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_api.domain import topics as topic_rules
from memory_api.domain.errors import ConflictError, MemoryFault, NotFoundError, SecretError
from memory_api.domain.models import (
    Block,
    BlockInput,
    Decision,
    Memory,
    MemoryInput,
    Page,
    Selection,
    Topic,
    TopicDetail,
    TopicPage,
    TopicUpdate,
)
from memory_api.domain.secrets import refuse_secrets

if TYPE_CHECKING:
    from collections.abc import Callable

SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA secure_delete=ON;
CREATE TABLE IF NOT EXISTS memories (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL,
 profile TEXT,
 session_id TEXT,
 scope TEXT NOT NULL,
 kind TEXT NOT NULL,
 title TEXT NOT NULL,
 body TEXT NOT NULL,
 value_json TEXT NOT NULL,
 source TEXT NOT NULL,
 asserted_by TEXT NOT NULL,
 trust TEXT NOT NULL,
 confidence REAL NOT NULL,
 importance INTEGER NOT NULL,
 occurred_at REAL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 last_accessed_at REAL NOT NULL,
 access_count INTEGER NOT NULL,
 confirmed_at REAL,
 valid_from REAL,
 valid_to REAL,
 supersedes_id TEXT,
 superseded_by_id TEXT,
 expires_at REAL,
 forgotten_at REAL,
 revision INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS memory_account ON memories(account_id, profile, session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(
 memory_id UNINDEXED, account_id UNINDEXED, title, body, value
);
CREATE TABLE IF NOT EXISTS memory_blocks (
 account_id TEXT NOT NULL, label TEXT NOT NULL, body TEXT NOT NULL,
 char_limit INTEGER NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(account_id, label)
) STRICT;
CREATE TABLE IF NOT EXISTS memory_links (
 account_id TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT NOT NULL, relation TEXT NOT NULL,
 PRIMARY KEY(account_id, from_id, to_id, relation)
) STRICT;
CREATE TABLE IF NOT EXISTS memory_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, at REAL NOT NULL,
 action TEXT NOT NULL, memory_id TEXT, detail TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS topics (
 id TEXT PRIMARY KEY,
 account_id TEXT NOT NULL,
 profile TEXT,
 key TEXT NOT NULL,
 title TEXT NOT NULL,
 summary TEXT NOT NULL,
 kind TEXT NOT NULL,
 first_seen REAL NOT NULL,
 last_summarised_at REAL,
 revision INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS topic_key ON topics(account_id, IFNULL(profile,''), key);
"""

VOUCHED_FOR = "(m.trust<>'untrusted' OR m.confirmed_at IS NOT NULL)"
"""What makes a memory usable: it did not come from somewhere anyone could write, or
somebody has since said it is right. Retrieval and the topic index must agree on this,
because a topic reaching the index puts its title into a prompt, and a title is exactly
what an attacker who can write a page would like to choose."""

# Every read of the index recomputes membership. See the docstring on `Topic` for why a
# stored count is not good enough. The join is inner on purpose: a topic whose memories
# have all been forgotten or superseded stops existing, rather than lingering as a title
# with nothing behind it. The HAVING clause is the security boundary -- a topic made
# entirely of untrusted memories never reaches the index, because its own title came from
# the untrusted content and putting that in a prompt is the attack it is there to prevent.
TOPIC_COLUMNS = (
    """
 t.id, t.account_id, t.profile, t.key, t.title, t.summary, t.kind, t.first_seen,
 t.last_summarised_at, t.revision,
 SUM(CASE WHEN """
    + VOUCHED_FOR
    + """ THEN 1 ELSE 0 END) AS memory_count,
 SUM(CASE WHEN NOT """
    + VOUCHED_FOR
    + """ THEN 1 ELSE 0 END) AS unconfirmed,
 MAX(m.last_accessed_at) AS last_seen,
 MAX(m.importance) AS importance
"""
)
TOPIC_JOIN = """
 FROM topics t JOIN memories m
   ON m.topic_id=t.id AND m.forgotten_at IS NULL AND m.superseded_by_id IS NULL
"""
TOPIC_HAVING = " GROUP BY t.id HAVING memory_count>0"

DATABASE_FILE_MODE = 0o600
SIDECARS = ("-wal", "-shm")

HALF_LIFE_SECONDS = 30 * 86400
MIN_MERGE = 2
"""A lone idle memory is left alone; merging one fact into a summary of itself is noise."""
"""How long until a memory's recency term is worth half what it was.

A plain ``exp(-dt / tau)`` would make this an e-folding constant rather than a half-life,
and the weight would halve at about twenty-one days instead of thirty. The name is the one
people reason about, so the arithmetic below carries the ``ln 2`` that makes it true."""


class SQLStore:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # The clock is set before the migration runs, not after: the backfill below writes
        # rows, and writing a row needs to know what time it is.
        self.clock = clock
        self.db.executescript(SCHEMA)
        self._make_private(path)
        self._add_missing_columns()

    @staticmethod
    def _make_private(path: str) -> None:
        """Let only the account running this process read the file, and its sidecars.

        The content is a person's own words about themselves, which makes it at least as
        sensitive as anything the siblings hold, and it was being created at whatever the
        process umask happened to be. Applied after opening rather than before, because
        there is nothing to change the mode of until SQLite has created the file.
        """
        if path == ":memory:":
            return
        database = Path(path)
        sidecars = (database.with_name(database.name + suffix) for suffix in SIDECARS)
        for candidate in (database, *sidecars):
            # chmod is advisory on Windows and unsupported on some filesystems. Where it
            # does nothing it is the operator's directory permissions that matter.
            with contextlib.suppress(OSError, NotImplementedError):
                candidate.chmod(DATABASE_FILE_MODE)

    def _add_missing_columns(self) -> None:
        """Bring an older database forward.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
        column added after the first release would silently never appear. Checking is
        cheap, runs once at startup, and turns a confusing runtime error into no error.
        """
        present = {row["name"] for row in self.db.execute("PRAGMA table_info(memories)")}
        if "topic_id" not in present:
            self.db.execute("ALTER TABLE memories ADD COLUMN topic_id TEXT")
        self._backfill_topics()

    def _backfill_topics(self) -> None:
        """Give every memory written before the topic layer a topic.

        Without this the index is an inner join against rows whose `topic_id` is still
        null, so an upgraded database reports no topics at all -- and keeps reporting none
        until every memory happens to be rewritten. What the person reads is an assistant
        that has forgotten everything about them, which is the worst possible way for a
        migration to fail. Running it at startup costs one pass over rows that have no
        topic, which is every row exactly once and none of them ever again.
        """
        rows = self.db.execute(
            "SELECT * FROM memories WHERE topic_id IS NULL ORDER BY sequence"
        ).fetchall()
        if not rows:
            return
        with self.db:
            for row in rows:
                memory = self._decode(row)
                memory.topic_id = self._assign_topic(memory, None)
                self._save(memory)

    def ping(self) -> None:
        """Touch the database the way a request would, for readiness to report on.

        A read against a real table rather than ``SELECT 1``: the latter answers from
        the connection alone and would keep saying yes after the file underneath it
        had been deleted or its permissions changed.
        """
        self.db.execute("SELECT 1 FROM memories LIMIT 1").fetchone()

    def close(self) -> None:
        self.db.close()

    def _event(self, account: str, action: str, memory_id: str | None) -> None:
        self.db.execute(
            "INSERT INTO memory_events(account_id,at,action,memory_id,detail) VALUES(?,?,?,?,?)",
            (account, self.clock(), action, memory_id, "{}"),
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> Memory:
        values = dict(row)
        del values["sequence"]
        values["value"] = json.loads(values.pop("value_json"))
        return Memory.model_validate(values)

    def _save(self, memory: Memory) -> None:
        values = memory.model_dump()
        values["value_json"] = json.dumps(values.pop("value"), sort_keys=True)
        columns = list(values)
        assignments = ",".join(f"{column}=excluded.{column}" for column in columns)
        self.db.execute(
            f"INSERT INTO memories({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            list(values.values()),
        )
        self.db.execute(
            "DELETE FROM memory_search WHERE account_id=? AND memory_id=?",
            (memory.account_id, memory.id),
        )
        self.db.execute(
            "INSERT INTO memory_search(memory_id,account_id,title,body,value) VALUES(?,?,?,?,?)",
            (memory.id, memory.account_id, memory.title, memory.body, values["value_json"]),
        )

    def get(self, account: str, memory_id: str) -> Memory:
        row = self.db.execute(
            "SELECT * FROM memories WHERE account_id=? AND id=?", (account, memory_id)
        ).fetchone()
        if row is None:
            message = "Memory not found for this account."
            raise NotFoundError(message)
        return self._decode(row)

    def _write(
        self, account: str, asserted_by: str, request: MemoryInput, target: str | None = None
    ) -> Memory:
        refuse_secrets(request.model_dump())
        now = self.clock()
        previous = None if target is None else self.get(account, target)
        if previous is not None:
            if previous.forgotten_at is not None or previous.superseded_by_id is not None:
                message = "Only a current, remembered memory can be corrected."
                raise ConflictError(message)
            if (request.scope, request.profile, request.session_id) != (
                previous.scope,
                previous.profile,
                previous.session_id,
            ):
                message = "A correction must preserve the original memory scope."
                raise ConflictError(message)
            if request.valid_from is not None and request.valid_from < (previous.valid_from or 0):
                message = "A correction cannot start before the memory it replaces."
                raise ConflictError(message)
        candidates = self.db.execute(
            "SELECT * FROM memories WHERE account_id=? AND scope=? AND profile IS ? "
            "AND session_id IS ? AND kind=? AND title=? AND body=? AND value_json=? "
            "AND forgotten_at IS NULL AND superseded_by_id IS NULL AND trust=?",
            (
                account,
                request.scope,
                request.profile,
                request.session_id,
                request.kind,
                request.title,
                request.body,
                json.dumps(request.value, sort_keys=True),
                request.trust,
            ),
        ).fetchall()
        if candidates and previous is None:
            return self._refresh(self._decode(candidates[0]), request)
        memory = Memory(
            **request.model_dump(exclude={"valid_from"}),
            id="mem_" + secrets.token_hex(16),
            account_id=account,
            asserted_by=asserted_by,
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
            valid_from=now if request.valid_from is None else request.valid_from,
            supersedes_id=target,
        )
        if previous is not None:
            previous.valid_to = memory.valid_from
            previous.superseded_by_id = memory.id
            previous.updated_at = now
            previous.revision += 1
            self._save(previous)
            self.db.execute(
                "INSERT INTO memory_links VALUES(?,?,?,?)",
                (account, memory.id, previous.id, "supersedes"),
            )
        memory.topic_id = self._assign_topic(memory, previous)
        self._save(memory)
        self._event(account, "add" if previous is None else "correct", memory.id)
        return memory

    def _assign_topic(self, memory: Memory, previous: Memory | None) -> str:
        """Which topic this memory joins.

        A correction inherits the topic of what it corrects. That is not an optimisation:
        "I moved to Bristol" and "I live in London" are the same subject, and letting the
        matcher decide again would sometimes split a fact from its own history.
        """
        if previous is not None and previous.topic_id is not None:
            return previous.topic_id
        key = topic_rules.key_for(memory.title)
        rows = self.db.execute(
            "SELECT id,key FROM topics WHERE account_id=? AND profile IS ?",
            (memory.account_id, memory.profile),
        ).fetchall()
        chosen = topic_rules.choose(key, {str(row["key"]): str(row["id"]) for row in rows})
        if chosen is not None:
            return chosen
        topic_id = "top_" + secrets.token_hex(16)
        self.db.execute(
            "INSERT INTO topics(id,account_id,profile,key,title,summary,kind,first_seen,"
            "last_summarised_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                topic_id,
                memory.account_id,
                memory.profile,
                key,
                topic_rules.clamp(memory.title, topic_rules.MAX_TITLE),
                topic_rules.summarise(memory.title, memory.body),
                memory.kind,
                self.clock(),
                None,
                1,
            ),
        )
        self._event(memory.account_id, "topic_created", memory.id)
        return topic_id

    def topics(self, account: str, profile: str | None = None, limit: int = 50) -> TopicPage:
        """The index: one line per subject, newest activity first."""
        rows = self.db.execute(
            "SELECT"
            + TOPIC_COLUMNS
            + TOPIC_JOIN
            + " WHERE t.account_id=? AND (t.profile IS NULL OR t.profile IS ?)"
            + TOPIC_HAVING
            + " ORDER BY last_seen DESC, t.id",
            (account, profile),
        ).fetchall()
        chosen = [Topic.model_validate(dict(row)) for row in rows[:limit]]
        return TopicPage(data=chosen, has_more=len(rows) > limit, total=len(rows))

    def topic(self, account: str, topic_id: str, limit: int = 50) -> TopicDetail:
        """One topic and what is in it -- the expansion the index exists to make cheap."""
        row = self.db.execute(
            "SELECT"
            + TOPIC_COLUMNS
            + TOPIC_JOIN
            + " WHERE t.account_id=? AND t.id=?"
            + TOPIC_HAVING,
            (account, topic_id),
        ).fetchone()
        if row is None:
            message = "Topic not found for this account."
            raise NotFoundError(message)
        # VOUCHED_FOR is a module constant, never anything a caller can reach.
        query = (
            "SELECT * FROM memories AS m WHERE m.account_id=? AND m.topic_id=? "  # noqa: S608
            f"AND m.forgotten_at IS NULL AND m.superseded_by_id IS NULL AND {VOUCHED_FOR} "
            "ORDER BY last_accessed_at DESC LIMIT ?"
        )
        members = self.db.execute(query, (account, topic_id, limit)).fetchall()
        return TopicDetail(
            topic=Topic.model_validate(dict(row)),
            memories=[self._decode(member) for member in members],
        )

    def update_topic(self, account: str, topic_id: str, request: TopicUpdate) -> Topic:
        """Retitle or resummarise. Membership is never editable from here."""
        refuse_secrets(request.model_dump())
        current = self.topic(account, topic_id).topic
        with self.db:
            self.db.execute(
                "UPDATE topics SET title=?,summary=?,last_summarised_at=?,revision=revision+1 "
                "WHERE account_id=? AND id=?",
                (
                    topic_rules.clamp(request.title or current.title, topic_rules.MAX_TITLE),
                    topic_rules.clamp(request.summary or current.summary, topic_rules.MAX_SUMMARY),
                    self.clock(),
                    account,
                    topic_id,
                ),
            )
            self._event(account, "topic_summarised", None)
        return self.topic(account, topic_id).topic

    # The fields the duplicate check does not compare. Two writes agreeing on the claim --
    # same scope, kind, title, body, value and trust -- are the same memory, and a second
    # row would be a near-duplicate of exactly the kind reconciliation exists to prevent.
    # But the caller may well have revised its assessment of that claim, and returning the
    # stored row unchanged threw those revisions away without a word: a caller setting an
    # expiry on something it had written before got a memory that never expires and no
    # indication of it.
    _REVISABLE = ("importance", "confidence", "source", "occurred_at", "expires_at")

    def _refresh(self, stored: Memory, request: MemoryInput) -> Memory:
        """Fold a repeated write's revised metadata into the memory it duplicates."""
        changes = {
            field: getattr(request, field)
            for field in self._REVISABLE
            if getattr(request, field) != getattr(stored, field)
        }
        if not changes:
            return stored
        for field, value in changes.items():
            setattr(stored, field, value)
        stored.updated_at = self.clock()
        stored.revision += 1
        self._save(stored)
        self._event(stored.account_id, "revise", stored.id)
        return stored

    def write(
        self, account: str, asserted_by: str, request: MemoryInput, target: str | None = None
    ) -> Memory:
        with self.db:
            return self._write(account, asserted_by, request, target)

    def _transition(self, account: str, memory_id: str, action: str) -> Memory:
        memory = self.get(account, memory_id)
        now = self.clock()
        if action == "forget":
            memory.forgotten_at = now if memory.forgotten_at is None else memory.forgotten_at
        elif action == "restore":
            memory.forgotten_at = None
        else:
            # Confirmation records that somebody vouched for the claim. It does not change
            # where the claim came from, and overwriting `trust` here used to do exactly
            # that: an inferred memory came back labelled as something the person stated.
            # Retrieval gates on `confirmed_at` instead, so an untrusted memory still has to
            # be vouched for before it is used, and still remembers that it was untrusted.
            memory.confirmed_at = now
        memory.updated_at = now
        memory.revision += 1
        self._save(memory)
        self._event(account, action, memory_id)
        return memory

    def transition(self, account: str, memory_id: str, action: str) -> Memory:
        with self.db:
            return self._transition(account, memory_id, action)

    def batch(self, account: str, asserted_by: str, decisions: list[Decision]) -> list[Memory]:
        result = []
        with self.db:
            for decision in decisions:
                if decision.action in {"ADD", "UPDATE"}:
                    result.append(
                        self._write(
                            account,
                            asserted_by,
                            MemoryInput.model_validate(decision.memory),
                            decision.memory_id if decision.action == "UPDATE" else None,
                        )
                    )
                elif decision.action == "DELETE":
                    result.append(self._transition(account, str(decision.memory_id), "forget"))
                else:
                    result.append(self.get(account, str(decision.memory_id)))
        return result

    def _where(
        self, account: str, selection: Selection, *, retrieval: bool
    ) -> tuple[str, list[Any]]:
        clauses = ["account_id=?"]
        args: list[Any] = [account]
        if selection.profile is not None or retrieval:
            clauses.append("(scope='account' OR (profile=? AND (scope='profile' OR session_id=?)))")
            args.extend((selection.profile, selection.session_id))
        if not selection.include_forgotten or retrieval:
            clauses.append("forgotten_at IS NULL")
        at = self.clock() if selection.as_of is None else selection.as_of
        if not selection.include_history or retrieval:
            clauses.append("valid_from<=? AND (valid_to IS NULL OR valid_to>?)")
            args.extend((at, at))
        if retrieval:
            # Untrusted until vouched for. Gating on `confirmed_at` rather than on trust
            # keeps the provenance intact: see `_transition`.
            clauses.append(
                VOUCHED_FOR.replace("m.", "") + " AND (expires_at IS NULL OR expires_at>?)"
            )
            # A summary is semantic memory -- it is what a consolidation pass produces --
            # so it crosses sessions like a fact does. An episode does not: "we talked
            # about tour dates on Tuesday" is a thing that happened, not a thing that is
            # the case, and surfacing it three weeks later is noise.
            clauses.append(
                "(kind IN ('fact','procedure','summary') OR (scope='session' AND session_id=?))"
            )
            args.extend((at, selection.session_id))
        if not selection.include_inferred:
            clauses.append("trust<>'inferred'")
        if not selection.include_stated:
            clauses.append("trust<>'stated'")
        for cursor, sign in ((selection.after, ">"), (selection.before, "<")):
            if cursor is not None:
                self.get(account, cursor)
                actual_sign = sign if selection.order == "asc" else {">": "<", "<": ">"}[sign]
                # S608: the only interpolation is a comparison operator chosen from two
                # literals here; the cursor itself is bound.
                clauses.append(
                    f"sequence{actual_sign}"  # noqa: S608
                    "(SELECT sequence FROM memories WHERE account_id=? AND id=?)"
                )
                args.extend((account, cursor))
        return " AND ".join(clauses), args

    def listing(
        self, account: str, selection: Selection, *, retrieval: bool = False
    ) -> Page[Memory]:
        if retrieval and (selection.after is not None or selection.before is not None):
            # Cursors bound the query by insert order; retrieval then re-sorts by rank in
            # Python. Combining them pages through one ordering while presenting another,
            # which is worse than refusing: a caller would believe it had seen everything
            # above a threshold when it had seen everything written after a row.
            message = (
                "Cursors page by write order and retrieval ranks by relevance. "
                "Page with the listing view, or raise the limit here."
            )
            raise MemoryFault(message)
        where, args = self._where(account, selection, retrieval=retrieval)
        scores: dict[str, float] = {}
        if selection.q is not None:
            words = selection.q.split()
            if not words:
                message = "Search needs at least one word."
                raise MemoryFault(message)
            # Quote every term so FTS operators and punctuation are data, never a query language.
            query = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
            hits = self.db.execute(
                "SELECT memory_id,bm25(memory_search) FROM memory_search "
                "WHERE memory_search MATCH ? AND account_id=?",
                (query, account),
            ).fetchall()
            scores = {str(row[0]): -float(row[1]) for row in hits}
            if not scores:
                return Page[Memory](data=[])
            where += " AND id IN (" + ",".join("?" for _ in scores) + ")"
            args.extend(scores)
        rows = self.db.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY sequence {selection.order}",  # noqa: S608
            args,
        ).fetchall()
        memories = [self._decode(row) for row in rows]
        if retrieval:
            self._rank(memories, scores)
        chosen = memories[: selection.limit]
        if retrieval:
            with self.db:
                for memory in chosen:
                    memory.last_accessed_at = self.clock()
                    memory.access_count += 1
                    self._save(memory)
        return Page[Memory](
            data=chosen,
            has_more=len(memories) > selection.limit,
            first_id=chosen[0].id if chosen else None,
            last_id=chosen[-1].id if chosen else None,
        )

    def _rank(self, memories: list[Memory], scores: dict[str, float]) -> None:
        if not memories:
            return
        components = [
            [
                scores.get(memory.id, 0),
                math.exp(
                    -math.log(2)
                    * max(0, self.clock() - memory.last_accessed_at)
                    / HALF_LIFE_SECONDS
                ),
                memory.importance / 10,
            ]
            for memory in memories
        ]
        minima = [min(values) for values in zip(*components, strict=True)]
        maxima = [max(values) for values in zip(*components, strict=True)]
        ranked = {
            memory.id: sum(
                (value - low) / (high - low) if high > low else 0
                for value, low, high in zip(values, minima, maxima, strict=True)
            )
            for memory, values in zip(memories, components, strict=True)
        }
        memories.sort(key=lambda memory: (-ranked[memory.id], -memory.confidence, memory.id))

    def blocks(self, account: str) -> list[Block]:
        rows = self.db.execute(
            "SELECT label,body,char_limit,updated_at FROM memory_blocks WHERE account_id=? "
            "ORDER BY label",
            (account,),
        ).fetchall()
        return [Block.model_validate(dict(row)) for row in rows]

    def block(self, account: str, label: str, request: BlockInput | None = None) -> Block:
        if request is not None:
            refuse_secrets(request.model_dump())
            with self.db:
                self.db.execute(
                    "INSERT INTO memory_blocks VALUES(?,?,?,?,?) ON CONFLICT(account_id,label) "
                    "DO UPDATE SET body=excluded.body,char_limit=excluded.char_limit,"
                    "updated_at=excluded.updated_at",
                    (account, label, request.body, request.char_limit, self.clock()),
                )
                self._event(account, "block_write", None)
        row = self.db.execute(
            "SELECT label,body,char_limit,updated_at FROM memory_blocks "
            "WHERE account_id=? AND label=?",
            (account, label),
        ).fetchone()
        if row is None:
            message = "Memory block not found for this account."
            raise NotFoundError(message)
        return Block.model_validate(dict(row))

    def delete_block(self, account: str, label: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM memory_blocks WHERE account_id=? AND label=?", (account, label)
            )
            self._event(account, "block_delete", None)

    def forget_all(self, account: str) -> int:
        """Forget everything, and report how many memories were still current.

        The count excludes rows that had already been superseded by a correction. They are
        forgotten too, but they were history rather than something the assistant still
        believed, and counting them would tell a person they had far more remembered about
        them than they did.
        """
        with self.db:
            now = self.clock()
            current = self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE account_id=? AND forgotten_at IS NULL "
                "AND superseded_by_id IS NULL",
                (account,),
            ).fetchone()[0]
            self.db.execute(
                "UPDATE memories SET forgotten_at=?,updated_at=?,revision=revision+1 "
                "WHERE account_id=? AND forgotten_at IS NULL",
                (now, now, account),
            )
            changed = int(current)
            self.db.execute("DELETE FROM memory_blocks WHERE account_id=?", (account,))
            self._event(account, "forget_all", None)
            return changed

    def sweep(self, grace_seconds: float) -> int:
        rows = self.db.execute(
            "SELECT account_id,id FROM memories WHERE forgotten_at IS NOT NULL AND forgotten_at<=?",
            (self.clock() - grace_seconds,),
        ).fetchall()
        with self.db:
            for row in rows:
                account, memory_id = row
                self.db.execute("DELETE FROM memories WHERE account_id=? AND id=?", row)
                self.db.execute("DELETE FROM memory_search WHERE account_id=? AND memory_id=?", row)
                self.db.execute(
                    "DELETE FROM memory_links WHERE account_id=? AND (from_id=? OR to_id=?)",
                    (account, memory_id, memory_id),
                )
                self._event(account, "erase", memory_id)
        # A truncated WAL prevents erased plaintext remaining in checkpointed journal pages.
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(rows)

    def consolidate(self, idle_seconds: float) -> int:
        """Merge idle current facts in one topic into a summary row.

        Returns how many summary rows were written. A window of zero or less is a no-op:
        rewriting everything the moment it is written is not consolidation, it is noise.
        """
        if idle_seconds <= 0:
            return 0
        cutoff = self.clock() - idle_seconds
        rows = self.db.execute(
            "SELECT * FROM memories WHERE forgotten_at IS NULL AND superseded_by_id IS NULL "
            "AND last_accessed_at<=? AND trust<>'untrusted' "
            "AND kind IN ('fact','procedure','summary') "
            "ORDER BY account_id, topic_id, created_at, id",
            (cutoff,),
        ).fetchall()
        groups: dict[tuple[str, str], list[Memory]] = {}
        for row in rows:
            memory = self._decode(row)
            topic_id = memory.topic_id
            if topic_id is None:
                continue
            groups.setdefault((memory.account_id, topic_id), []).append(memory)
        written = 0
        with self.db:
            for (account, topic_id), members in groups.items():
                if len(members) < MIN_MERGE:
                    continue
                if self._merge_topic(account, topic_id, members):
                    written += 1
        return written

    def _merge_topic(self, account: str, topic_id: str, members: list[Memory]) -> bool:
        """Write one summary and retire the members it replaces. False when the body is refused."""
        now = self.clock()
        title = members[0].title
        body = topic_rules.clamp("; ".join(item.body for item in members if item.body), 16_000)
        request = MemoryInput(
            title=title,
            body=body,
            kind="summary",
            source="consolidation",
            trust="inferred",
            importance=max(item.importance for item in members),
            scope=members[0].scope,
            profile=members[0].profile,
            session_id=members[0].session_id,
        )
        try:
            refuse_secrets(request.model_dump())
        except SecretError:
            return False
        summary = self._write(account, "memory-api", request)
        summary.topic_id = topic_id
        self._save(summary)
        for previous in members:
            previous.valid_to = now
            previous.superseded_by_id = summary.id
            previous.updated_at = now
            previous.revision += 1
            self._save(previous)
            self.db.execute(
                "INSERT INTO memory_links VALUES(?,?,?,?)",
                (account, summary.id, previous.id, "supersedes"),
            )
        self.db.execute(
            "UPDATE topics SET summary=?, last_summarised_at=?, revision=revision+1 WHERE id=?",
            (topic_rules.summarise(title, body), now, topic_id),
        )
        self._event(account, "consolidate", summary.id)
        return True
