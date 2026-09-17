# Architecture

One process, one SQLite file, one thread that touches it, and five packages pointing
inward. This page explains *why* the shape is what it is; [AGENTS.md](../AGENTS.md) is the
operating manual for working inside it.

## The shape

```
src/memory_api/
  api/      routers, dependencies, wire schemas, problem+json errors, middleware
  auth/     token verification: a thin adapter over the family's keyring_client
  core/     configuration, the composition root, the request-id context
  domain/   the models, the topic rules, the secret refusal, the error vocabulary
  store/    the schema and every query, behind one worker thread
```

Dependencies point inward. `domain/` is pure — models, clustering rules and the credential
check, with no I/O, no clock and no configuration — which is what lets the rules be read and
tested as functions. `core/container.py` is the composition root: the JWKS client, the
verifier and the store worker are constructed there, once, and handed to the app, so every
part of the service is testable by substitution.

`api/schemas.py` and `domain/models.py` are separate on purpose. The domain models are what
the store reads and writes; the schemas are the envelopes around them. The HTTP contract is
public and its operation ids are MCP tool names, so it has to be able to grow a field
without the storage layer having an opinion about it.

### One import contract, not five

`lint-imports` enforces exactly one rule, inside `make check`: **routers may not import
`keyring_client`, `httpx` or `jwt`.** Indirect imports are allowed, so a router still
reaches the verifier through a dependency.

It is the one rule whose violation would be invisible. This service's entire relationship
with keyring is "verify this signed token against those published keys" — no call to
keyring at request time, ever — and the way that ends is somebody needing a fact keyring
holds, adding one `httpx` call in a handler, and it being reviewed and merged. The rest of
the layering is held up by there being five packages you can read in an afternoon.

## A request, end to end

1. **`RequestContextMiddleware`** is outermost. It binds a request id — the caller's
   `X-Request-ID` if there is one, capped at 64 characters, otherwise a fresh one — and
   echoes it on the way out. It also catches an unhandled exception itself rather than
   leaving it to Starlette's outer error middleware, because that one runs after the
   binding has unwound and the 500 it produces would carry no id. The id is the only thing
   a 500 body gives the caller, because the body deliberately says nothing else.
2. **Dependencies** establish who is asking. `CurrentCallerDep` is the only way a route
   learns whose memories it is touching. A missing bearer is this service's own
   `AuthenticationError` and a problem-shaped 401, not FastAPI's default.
3. **`auth/`** verifies the token locally with `keyring_client` and turns the library's
   verdicts into this service's errors, so nothing above it knows a library was involved:
   every refusal is one identical 401, and keys that cannot be fetched are a 503.
4. **The router** hands a closure to `StoreWorker.call` and awaits it. Handlers contain no
   SQL and no error mapping; they raise domain errors and let `api/errors.py` decide what
   that means over HTTP.
5. **The store** runs the closure on its own thread, inside a transaction.

## One SQLite file, on one dedicated thread

`SQLStore` is synchronous. Called from a route it would block the event loop for the length
of a transaction, and under concurrency that is not a slow request but a stalled process —
nothing else is served, `/healthy` included.

So it lives behind a `ThreadPoolExecutor` of exactly **one** thread and is reached only
through `StoreWorker.call`. The connection is not constructed in `__init__` and handed over;
it is opened by a job submitted to that pool, because the thread that opens a SQLite
connection is the thread that must use it.

That property is *enforced*, not merely intended. `sqlite3.connect` defaults to
`check_same_thread=True` and the store leaves it alone, so a call that escapes the worker
raises `ProgrammingError` at once instead of quietly corrupting a transaction under load.
Turning it off would make the discipline unfalsifiable, and there is a test that trips the
guard deliberately to prove it is still armed.

**Why not async SQLite.** The asynchronous SQLite libraries are this design with a
different spelling: a background thread per connection, with the blocking moved rather than
removed. What would be gained is an API; what would be lost is the single place where the
thread discipline is visible.

**Why not a pool.** SQLite has one writer. A second connection writing gets `SQLITE_BUSY`,
and then you own retry and backoff logic for a contention this service can simply not have.
Serialising in the process makes every transaction indivisible by construction rather than
by convention — which is what the batch route's atomicity depends on.

**The tradeoff, plainly.** Throughput is one transaction at a time, and a long transaction
delays every other request behind it. For a store whose working set is one person's
memories that is a long way from the limit, and the honest answer when it stops being true
is more processes over partitioned data, not a pool over one file.

## The schema

`STRICT` tables, WAL journalling, and `secure_delete=ON` so erased pages are overwritten
rather than merely unlinked.

| Table | Holds |
| --- | --- |
| `memories` | Every version of every memory, current and superseded, remembered and forgotten. `sequence` is the autoincrement key cursors compare on; `id` is the public one. |
| `memory_search` | The FTS5 index over title, body and serialised value. |
| `memory_blocks` | The always-in-context blocks, keyed `(account_id, label)`. |
| `topics` | One row per subject, unique on `(account_id, profile, key)`. |
| `memory_links` | `supersedes` edges, written when a correction lands. |
| `memory_events` | Append-only: what happened, when, to which id — `add`, `correct`, `forget`, `restore`, `confirm`, `erase`, `topic_created`, `topic_summarised`, `block_write`, `block_delete`, `forget_all`. |

`memory_links` and `memory_events` have **no read surface yet** — nothing queries them, and
no route returns them. They are written because the record is worth having from the first
release rather than from the first release that needed it. Two things follow: the event log
holds ids and actions and never text, so it can outlive the memories it describes; and the
sweeper removes a memory's links when it erases the row but leaves the event that says it
was erased.

`PRAGMA foreign_keys=ON` is set and no table declares one. Keeping it on costs nothing and
means the first foreign key to be added behaves.

Startup runs `_add_missing_columns`, which adds `topic_id` if an older database lacks it.
`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so without this a
column added after the first release would silently never appear.

## Corrections are append-only, and nothing is hard-deleted

A correction does not overwrite. It writes a new row and closes the old one:

| Column | On the retired memory | On the correction |
| --- | --- | --- |
| `valid_to` | the correction's `valid_from` | `null` |
| `superseded_by_id` | the correction's id | `null` |
| `supersedes_id` | unchanged | the retired memory's id |
| `revision` | incremented | 1 |

Four things depend on the old row still being there, and all four are the point of the
service: `as_of` can answer what was true last year; a person can ask why the assistant
believes something; a forget is reversible; and a correction can be audited against what it
replaced. Delete-then-add gets you none of them, which is why `correct_memory` exists and
why the route description tells callers never to do the other thing.

Forgetting is a tombstone — `forgotten_at` — not a delete, so it can be undone. The **only**
thing that removes a memory row is `SQLStore.sweep`, which erases rows whose grace period
has run out, drops their index entries and links, and truncates the WAL so erased text does
not survive in checkpointed journal pages. [docs/operations.md](operations.md#erasure)
covers running it.

Blocks are the exception, and deliberately: `delete_memory_block` and `forget_all_memories`
remove block rows immediately, with no tombstone and no grace. A block is a small thing a
person edits by hand and rewrites wholesale, so versioning it would be machinery in the way
of the one operation it has.

## Search, and how retrieval ranks

The FTS5 index is written **by the store, explicitly, in the same transaction as the row**:
`_save` deletes the index entry and re-inserts it on every write. Not external-content
tables, which require issuing `'delete'` commands carrying the *old* values and corrupt
silently when one is missed; not triggers, which would have to render a JSON value to
searchable text in SQL. The explicit helper creates its own bug class — an index that
disagrees with the table — so there is a test asserting a corrected memory's old text is no
longer findable.

The index is shared across accounts by construction, so the account filter sits in the
`WHERE` beside the `MATCH`, and the ids it yields are then intersected with the main query,
which is account-scoped as well. `bm25()` ranks against the whole corpus rather than one
account's; at this scale that does not affect result quality and it leaks nothing, because
rows are filtered before they are returned.

Retrieval blends three components, each normalised across the candidate set before they are
added:

| Component | Value |
| --- | --- |
| Relevance | `-bm25(memory_search)`, or 0 for a search with no `q` |
| Recency | `exp(-(now - last_accessed_at) / 30 days)` |
| Importance | `importance / 10` |

Each is min-max normalised over the candidates in hand — so a component with no spread
contributes nothing rather than dividing by zero — and the three are summed. Ties break on
`confidence`, then on id, so two runs over the same data agree.

**The recency term decays from last access, not from creation**, and that is the choice the
whole ranking rests on. A person's sister's name is years old and matters every week; yesterday's
one-off note is new and matters once. Decaying from creation ranks them the wrong way round
and keeps doing it, evicting exactly the memories that have proved themselves. So retrieval
stamps `last_accessed_at` and increments `access_count` on everything it returns: use is
what keeps a memory near the top, and disuse is what lets it sink. (The decay constant is 30
days, applied as `exp(-Δt/τ)`: weight falls to 1/e after 30 days and to a half after about
21.)

Two costs come with it. Retrieval is a write, so a read path takes the write lock briefly.
And normalising needs the whole candidate set, so the query materialises every matching row
before slicing to `limit` — bounded by one account's memories, which is the right bound, but
it is a bound rather than a page.

## How topics cluster

A flat list of remembered facts cannot be summarised, and a model handed twenty unrelated
sentences cannot tell what it knows *about*. So a memory belongs to a topic, and what an
assistant carries every turn is the index rather than the contents.

Assignment is three steps, in `domain/topics.py`:

1. **A key.** The title's meaningful words, lowercased, stopwords dropped — but only when
   something survives them, so a memory titled "how to" still gets a key — then deduplicated
   and sorted. Sorted because "tea preferences" and "preferences: tea" are the same subject.
2. **Exact key, then similarity.** An exact match is not a judgement call. Otherwise the
   closest existing topic by Jaccard overlap of the word sets, if it scores at least 0.5,
   with ties broken by the key itself so that an assignment never depends on dictionary
   order.
3. **Otherwise a new topic**, titled from the memory and summarised with the first sentence
   of its body, clamped to one line.

Candidates are restricted to the same account **and the same profile**, so one profile's
memories never join another's topics. A correction inherits the topic of what it corrects
rather than being matched again: "I moved to Bristol" and "I live in London" are the same
subject, and re-matching would sometimes split a fact from its own history.

**Why word overlap rather than embeddings.** It is explainable: when a memory lands in the
wrong topic a person can see exactly why, because the rule is "these titles share these
words", where an embedding's answer to the same question is a number. It is deterministic,
which is what makes the behaviour testable at all. And it has no model dependency, so a
write never waits on a network call and a provider outage cannot stop somebody being
remembered.

The cost is real: word overlap will sometimes scatter one subject across two topics, and
sometimes merge two subjects whose titles share words. The threshold is set to be wrong in
the safer direction — scattering is untidy, merging produces a summary that is true of
neither. An embedding backend is a strictly better matcher and can replace `similarity`
without touching anything else, which is why that seam is one function.

## The index is a security boundary

Everything a topic reports is recomputed on every read, from the memories that are in it
right now, joined on `forgotten_at IS NULL AND superseded_by_id IS NULL`. There are no
stored counters: one drifts the moment a memory is forgotten, superseded or erased, and an
index that overstates what it holds sends the model looking for something that is not there.

The `HAVING memory_count > 0` clause is the boundary, and `memory_count` counts only
members that are **not** untrusted. So **a topic made entirely of unconfirmed memories never
reaches the index, and cannot be expanded either.** Anybody who can get a paragraph in front
of an extraction pass — a web page, a forwarded email — can propose a memory. Storing it is
fine. Naming a topic after it, and putting that name in front of the model every turn, is
the attack. Confirming one member is enough to bring the topic in; the rest stay counted
separately as `unconfirmed`.

## Isolation is structural

Every store method takes an `account_id` and it is not optional on any of them. Over HTTP
there is nowhere to put one: no path, no query parameter and no request body accepts an
account, `asserted_by` included, and request models forbid extra fields so an invented one
is a 422 rather than a silent ignore. A cross-account read is not refused, it is
unexpressible — and when an id that belongs to somebody else is asked for, the answer is
404, because a 403 would confirm that it exists.

## Credentials are refused at the door

`domain/secrets.py` runs on every memory, every block and every topic rewrite, over the
**serialised** request, so a key buried in a structured `value` is caught as readily as one
in prose. It matches known credential shapes — bearer headers, PEM private keys, `sk-`,
`ghp_`, `github_pat_`, `xox…`, `AKIA` prefixes, JWTs — and, for any run of 40 or more
token characters, refuses anything above a Shannon entropy of 4.5 bits per character.

The refusal names keyring and never repeats the input. There is no setting that disables
it: this is a memory service, not a vault, and the only thing worse than refusing a
legitimate sentence is storing somebody's API key in plaintext for ever.

## What this service is not

- **Not administrable.** There is no route that lists accounts, reads somebody else's
  memories, or acts on a person's behalf. An operator with the database file has the rows;
  an operator with the API has nothing.
- **Not multi-process.** One connection, one writer, one thread.
- **Not encrypted.** The database holds a person's own words in plaintext. See
  [docs/operations.md](operations.md).
- **Not a source of instructions.** Everything it returns is data. See
  [docs/mcp.md](mcp.md).
