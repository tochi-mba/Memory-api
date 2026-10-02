# AGENTS.md

Working notes for memory-api. Read this before editing.

## What this service is

**memory-api** is the family's memory: what is remembered about a person, with its
provenance, its history, and the topic index an assistant carries in context. It verifies
keyring JWTs locally and holds no third-party credentials -- it refuses to store anything
that looks like one.

## Commands

| Command | What it does |
| --- | --- |
| `make install` | Create the venv and install everything. |
| `make check` | Lint, types, import contracts, tests at 100% branch coverage. |
| `make run` | Serve on :8009 with reload. |
| `make test` | Tests only. |
| `make fmt` | Format and apply safe lint fixes. |
| `make docker` | Build `memory-api:local`. |

Copy `.env.example` to `.env` before `make run`; the settings are explained in
[docs/operations.md](docs/operations.md).

## Invariants

- `/healthy` does no I/O and never fails.
- `/ready` reports keyring JWKS **and** the database, and answers 503 when either is unusable.
- Every `/v1` route takes identity only from a verified keyring token: the Bearer token on
  the person-facing routes, `X-Keyring-User-Token` on `/v1/internal`, where the Bearer is a
  sibling's service token. No route -- and no request body -- can name an account. A
  memory belonging to somebody else answers 404.
- `asserted_by` is derived from the verified caller. `source` is a claim the caller makes;
  `asserted_by` is a fact the service knows. They are never conflated.
- The store is synchronous and runs only on `StoreWorker`'s single thread.
  `check_same_thread` stays on: it is the guard that proves the discipline holds.
- Every error is `application/problem+json` with a `request_id`, and `detail` never echoes
  the offending value.
- A topic made entirely of unconfirmed untrusted memories never reaches the index. Its
  title came from untrusted content, and the index goes into a prompt.
- Confirming a memory stamps `confirmed_at` and never rewrites `trust`. Retrieval and the
  index gate on the confirmation, so provenance survives being vouched for.
- `operation_id`s are public API -- they become MCP tool names. A contract test pins the set.
- settings-api is shown only a token this service has already verified, and only through
  `core/preferences.py`. A person's importance floor is never guessed: when it cannot be
  read, a write that adds a memory is a 503, and nothing that does not add one is failed.
- No `pragma: no cover`. No setting that disables verification.
