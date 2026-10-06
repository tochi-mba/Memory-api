# Changelog

All notable changes to memory-api are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A merged summary is as trusted as its least trusted member.** Consolidation stamped
  every merge `inferred`, though it only joins the members' own words and calls no model, so a
  person's stated facts dropped out of every stated-only search once they had gone a month
  unused. Facts they stated now stay `stated`; one `inferred` member still makes the merge
  `inferred`. The topic docstring no longer says a model writes a better summary during
  consolidation.
- **settings-client 0.4.2.** A 2xx answer the client cannot use -- a proxy's page, an empty
  body, a document from a newer settings-api -- is treated as an outage and degrades as one,
  instead of reaching this service as a 500.
- **settings-client 0.4.1.** A single-flight lock is dropped by the last caller out. Older
  clients kept the lock of every resolve that failed (an outage, a refused grant) for good,
  one per token, and keyring tokens rotate every few minutes.
- **A batch with no `ADD` never asks settings-api.** The batch route's preferences
  dependency resolved the person's settings for every batch, so a settings-api refusing
  this service (401/403) failed a batch of `UPDATE`, `DELETE` and `NOOP` decisions with a
  503, and a slow one held it up, although the floor has no say in any of them. The
  floor is now asked for only when a write needs it.
- **A repeat is never held to the importance floor.** Writing a claim already remembered
  revises the stored memory's importance, expiry and confidence and adds nothing, but the
  floor treated it as a new memory: lowering the importance of something remembered, or
  setting it to expire, through a repeat below the floor was a 422 saying nothing was
  stored, and the floor made memory-api keep more about the person rather than less. The
  store now decides, in the transaction that applies the write, whether it adds a memory,
  and holds only that to the floor; a repeat, and a batch whose `ADD`s all repeat, never
  asks settings-api.

### Added

- **A person's importance floor.** With `MEMORY_SETTINGS_API_BASE_URL` and
  `MEMORY_SETTINGS_API_TOKEN` set, a new memory -- `create_memory`,
  `internal_create_memory`, or an `ADD` in `reconcile_memories` -- is held to the
  `memory.write_importance_floor` its owner chose in settings-api (a repeat of a
  remembered memory adds nothing and is not held to it). Below it is a 422
  `below-importance-floor` and nothing is stored; one `ADD` below it refuses the whole
  batch and names the decisions that were the reason. A correction is not held to it. The
  floor is never guessed: when settings-api cannot say, a write that adds a memory is a 503
  `preferences-unavailable`, while reads, forgets, corrections and a batch that adds nothing
  carry on. Unset, every write is kept exactly as before; set, a person who never chose gets
  the catalogue's default, which has to be 1 for nothing to change for them.
  `memory.consolidation` is not read:
  the idle-merge pass has no person's token to ask with, and there is no session-end signal.

- A GitHub Pages site at <https://tochi-mba.github.io/Memory-api/>, in the REX ink/signal style: what Memory-api is,
  its API, how to run it and what it will not do. `site/` is plain static HTML;
  `.github/workflows/pages.yml` publishes it after `scripts/check_site.py` has checked every
  page for a broken anchor, a missing asset, an image without alt text or draft text.
- The repository is attributed to REX Technologies: the LICENSE copyright holder, the package
  author and the README.
- **Memories.** Write, read, search, correct, confirm, forget, restore and erase, plus
  batch reconciliation so a caller can decide add, update, delete or no-op per candidate
  rather than blindly appending. Cursor pagination on the listing view. Every
  `operation_id` is stable public API because it becomes an MCP tool name, and a contract
  test pins the set.
- **Topics.** Memories cluster into named subjects with a title, a one-line summary and a
  count, so a caller can carry an index instead of the contents and expand only the
  subject it needs. `GET /v1/memory/topics` is the index, with an honest total so
  "showing 12 of 47" is always true; `GET /v1/memory/topics/{id}` expands one; `PATCH`
  lets a consolidation pass write a better title and summary without ever moving a memory
  between subjects.
- **Memory blocks.** A small, labelled, caller-editable block per account, addressable as
  its own resource, so what is pinned into a prompt can be inspected and edited directly
  rather than only through a model's answers.
- **Corrections that keep their history.** A correction writes a new row and closes the
  old one with `valid_to` and `superseded_by_id`, which is what makes temporal queries,
  "why do you think that?", and a reversible forget all possible at once.
- **Ranked retrieval.** FTS5 relevance, recency and importance, min-max normalised before
  they are combined. Recency decays from last access rather than creation, so a stable
  frequently used fact is not evicted by yesterday's one-off.
- **A write-time secret refusal.** Credential-shaped input is rejected with a message
  naming keyring, and the failure never echoes what was submitted.
- **Erasure that erases.** A grace period, a background sweep that actually runs, and a
  WAL truncate so deleted text does not linger in checkpointed journal pages.
  `MEMORY_FORGET_GRACE_SECONDS` and `MEMORY_SWEEP_INTERVAL_SECONDS` configure it.
- RFC 9457 `application/problem+json` errors with a `request_id` in the body and
  `X-Request-ID` on the response.
- **A two-credential `/v1/internal` surface.** A sibling presents its own token and the
  person's memory-api token. The account still comes from the person. Empty
  `MEMORY_SERVICE_TOKENS` refuses every caller. `operation_id`s are prefixed `internal_`
  so they are never MCP tool names.
- **Idle consolidation.** A background pass merges unused current facts in one topic into
  a `summary` row and supersedes the originals. `MEMORY_CONSOLIDATE_IDLE_SECONDS` and
  `MEMORY_CONSOLIDATE_INTERVAL_SECONDS` configure it.
- **A store that cannot block the event loop.** `StoreWorker` runs every SQLite call on one
  dedicated thread that owns the connection and constructs it there. `check_same_thread`
  stays on deliberately: it is what turns "we are careful about threads" into something a
  test can falsify. `/ready` gained a database check alongside the keyring one.
- `MEMORY_DATABASE_PATH` (default `var/memory.db`). The directory is created on first boot,
  and `:memory:` is accepted for throwaway databases.

### Changed

- **Confirming a memory no longer rewrites its provenance.** `confirm_memory` used to set
  `trust` to `stated` whatever it had been, so an untrusted claim scraped from a page came
  back labelled as something the person had said, and an inferred memory was relabelled as
  a statement. It now records `confirmed_at` and leaves `trust` alone; retrieval and the
  topic index gate on the confirmation instead, so an untrusted memory still has to be
  vouched for before it is used and still remembers what it was.
- **A correction rewrites its topic's line in the index.** A topic's summary was written
  once, from its first memory, and a correction joined the topic without touching it, so
  the index an assistant is told to believe went on stating what the correction said was
  wrong. A usable correction now rewrites the summary from its own body and clears
  `last_summarised_at`; an untrusted one rewrites nothing until it is confirmed.
- **`summary` memories are retrievable.** The retrieval filter admitted only `fact` and
  `procedure`, so the distilled output of a consolidation pass was the one kind that could
  never be read back. An account-scoped `episode` is still excluded, which was the intent.
- **Cursors are refused on the retrieval view** rather than silently paging by write order
  while presenting by rank. Page with the listing view.
- **`forget_all_memories` counts what was still believed**, not every row ever written. It
  was including superseded history, which is the one figure a person has for how much is
  held about them.
- The recency term now halves at `HALF_LIFE_SECONDS` rather than at about seven tenths of
  it. The constant was being used as an e-folding time while being named a half-life.
- The database file and its `-wal`/`-shm` sidecars are created `0600`. They were taking
  whatever the process umask gave them, which on many machines is world-readable.
- `confirm_memory`'s description, which is what an MCP client shows a model, said it
  promoted a memory from `untrusted` to `stated`. It has not done that since confirming
  stopped rewriting provenance; the description now says it stamps `confirmed_at` and
  leaves `trust` alone, on the internal route too.
- `search_memories`'s description listed four exclusions where retrieval applies six. It
  now names forgotten memories and other profiles' and sessions' memories too, and says
  that a search with no `profile` returns account-wide memories only.
- `internal_list_memories` declares its 404 in the OpenAPI document. A cursor naming a
  memory that is not the person's has always answered 404 there, as it does on
  `list_memories`, but the published contract left it out.
- **Consolidation stays inside a compartment.** It grouped idle memories by topic alone,
  and a profile's topic also holds each of its sessions' memories, so the summary took the
  oldest member's scope: one session's facts surfaced in every session of the profile, or
  were folded into another session and disappeared from their own. Only memories with the
  same scope, profile and session merge now.
- **A consolidation summary is written straight into its members' topic.** It went through
  the ordinary write path, which matched a topic by title: when the oldest member was a
  correction titled unlike its topic, a new topic was created and then left with nothing in
  it. The same path's duplicate check could return an earlier summary that was itself a
  member, which the merge then superseded with itself, leaving the topic nothing current.
- **A repeated write respects the `valid_from` it gives.** It was ignored, so the memory
  stayed valid from whenever it was first written, and an `expires_at` sent with it was
  folded onto that other start: an expiry before the start made a row that no longer
  decoded, and every listing of the account failed on it. The start is now revised like the
  other assessment fields, a correction's predecessor keeps meeting it, and an expiry that
  would not follow the stored start is refused.
- **The sweeper and the consolidator say what they did.** Each pass logs
  `sweep_completed erased=N` or `consolidation_completed summaries=N` at INFO, and a failed
  pass logs `sweep_failed` or `consolidation_failed` with the exception's type name at
  WARNING. Both were silent, success or failure. The `memory-api` entry point now routes
  the service's own loggers to uvicorn's handler, without which nothing below WARNING
  would have been printed.
- **An `expires_at` in the past with no `valid_from` is a 422, not a 500.** The request
  passed validation because it named no start, and the store's "now" was checked only by
  building the stored model, whose error nothing mapped. Writes, corrections, batch `ADD`
  and `UPDATE`, and the internal write routes now answer `invalid-memory` with
  `expires_at must follow valid_from`, the same refusal a repeated write gets, and store
  nothing.
- **Non-finite numbers are refused.** `NaN`, `Infinity`, `-Infinity` and literals that
  overflow to infinity (`1e400`) are a 422 `validation-failed` in every request body and
  query string: `valid_from`, `expires_at` and `occurred_at` on every write route, and
  `as_of` on both views. They were accepted, SQLite stored a NaN as NULL, and a memory
  written with `valid_from: NaN` was answered 201 and then never appeared anywhere. The
  OpenAPI document now describes each of those fields as finite epoch seconds.
- **Breaking:** `domain.errors.MemoryError` is now `MemoryFault`. The old name shadowed the
  Python builtin, and an `except MemoryError` written anywhere in the process -- here, in a
  dependency, in a pasted script -- would have caught whichever of the two was in scope,
  silencing the one that says the interpreter has run out of memory.

## [0.1.0] - 2026-09-16

### Added

- The service skeleton: configuration that refuses an unknown `MEMORY_*` variable at
  startup, local keyring JWT verification, `/healthy`, `/ready` and `GET /v1/whoami`.

### Changed

- **Breaking:** the floor is **Python 3.12**, which CI gates. Python 3.13 is declared
  supported but not yet gated. `.python-version`, `requires-python`, ruff's
  `target-version`, mypy's `python_version`, the Docker base image and the pre-commit
  interpreter all moved together, and `uv.lock` was regenerated. The family-wide reason is
  in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11.
