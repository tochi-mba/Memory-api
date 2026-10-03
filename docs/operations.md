# Operations

How to run memory-api, configure it, back it up, and erase what somebody asked to have
erased. Short, because there is not much of it: one process, one file, one outbound
dependency.

## What it needs

| | |
| --- | --- |
| Python | 3.12 or newer. 3.12 is the floor and what CI gates. |
| Disk | one SQLite file plus its `-wal` and `-shm` sidecars |
| Network, inbound | one port — **8009** in the family allocation — behind a TLS-terminating proxy |
| Network, outbound | keyring's JWKS URL, and settings-api when `MEMORY_SETTINGS_API_BASE_URL` is set |
| Secrets | none of its own; optional `MEMORY_SERVICE_TOKENS` admits siblings to `/v1/internal`, and `MEMORY_SETTINGS_API_TOKEN` when settings-api is on |

That last row used to be simpler. memory-api still holds no signing key and no
third-party credential, and it still refuses to store anything that looks like one. The
optional service-token map is proof that a *sibling* is asking, not a secret about a
person. What the database holds is a person's own words, which is a different kind of
sensitive and is what the rest of this page is mostly about.

## Running it locally

```bash
cp .env.example .env
make install
make check
make run          # http://127.0.0.1:8009/docs
```

`make run` serves with reload on 8009. `uv run memory-api` is the entry point a deployment
uses; it reads `MEMORY_HOST`, `MEMORY_PORT` and `MEMORY_LOG_LEVEL`.

A running keyring on 8001 is needed only for real tokens — the test suite mints its own
against `keyring_client.testing` and never touches the network. With one running, `KEYRING`
set to its base URL and `SESSION` to a session token from signing in to it:

```bash
TOKEN=$(curl -sX POST "$KEYRING/v1/auth/service-token" \
  -H "Authorization: Bearer $SESSION" -H 'Content-Type: application/json' \
  -d '{"audience": "memory-api"}' | jq -r .token)

curl -sH "Authorization: Bearer $TOKEN" http://127.0.0.1:8009/v1/whoami

curl -sX POST http://127.0.0.1:8009/v1/memory \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title": "Home city", "body": "Moved to Bristol"}'

curl -sH "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8009/v1/memory/search?q=city'
```

The audience must be exactly `MEMORY_AUDIENCE`. memory-api needs **no** entry in keyring's
`KEYRING_SERVICE_TOKENS` to verify person tokens: that list only admits services to
keyring's internal endpoints, which this service never calls. To *accept* Lucy on
`/v1/internal`, set `MEMORY_SERVICE_TOKENS` to a JSON object whose values are 32+ character
tokens you also give Lucy.

## In a container

```bash
make docker       # builds memory-api:local
docker run -p 8009:8009 --add-host=host.docker.internal:host-gateway \
  -e MEMORY_KEYRING_JWKS_URL=http://host.docker.internal:8001/.well-known/jwks.json \
  -e MEMORY_KEYRING_ISSUER=http://127.0.0.1:8001 \
  -v memory-data:/var/lib/memory memory-api:local
```

That reaches a keyring running on the host, which has to listen on an address the
container can reach (`KEYRING_HOST=0.0.0.0`), not only on loopback. Do not pass the `.env`
you copied from `.env.example` with `--env-file`: its `MEMORY_HOST=127.0.0.1` and relative
`MEMORY_DATABASE_PATH` override the image's values, so the container listens where the
port mapping cannot reach it and writes its database outside the volume.

The image runs as the non-root user `memory` (uid 10001) and bakes `MEMORY_HOST=0.0.0.0`,
`MEMORY_PORT=8009`, `MEMORY_DATABASE_PATH=/var/lib/memory/memory.db` and
`MEMORY_LOG_FORMAT=json`. `/var/lib/memory` is created `0700` and owned by that user before
it is declared a `VOLUME`, so a new named volume arrives writable. Its `HEALTHCHECK` calls
`/healthy`. The keyring issuer and JWKS URL are deliberately **not** baked in: they name the
keyring this deployment trusts, and a default in the image is how a container ends up
trusting the wrong one. Left unset, they fall back to the code defaults on `127.0.0.1`,
which inside a container is the container itself, so every authenticated request answers
503.

A volume created by an older image, before the directory was made in the image, stays
root-owned: docker only seeds ownership into an empty volume. `/ready` then reports the
database with reason `OperationalError`. Recreate the volume if it has never held data, or
`chown 10001:10001` its contents.

## In compose

The family compose file lives in the meta-repo, beside the service checkouts, and the hub
already expects this service at `http://memory:8009`. The block:

```yaml
  memory:
    build:
      context: ./Memory-api
      secrets: [github_token]
    ports:
      - "8009:8009"
    env_file:
      - .env.family
    environment:
      MEMORY_HOST: "0.0.0.0"
      MEMORY_PORT: "8009"
      MEMORY_DATABASE_PATH: /var/lib/memory/memory.db
      MEMORY_KEYRING_JWKS_URL: http://keyring:8001/.well-known/jwks.json
      MEMORY_KEYRING_ISSUER: "http://127.0.0.1:8001"
      MEMORY_LOG_FORMAT: json
    volumes:
      - memory-data:/var/lib/memory
    depends_on:
      keyring:
        condition: service_healthy
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8009/healthy', timeout=4).status == 200 else 1)",
        ]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
    networks: [lucy]
```

`MEMORY_SERVICE_TOKENS` arrives through `.env.family`: the meta-repo's `scripts/genenv.py`
writes a token for Lucy there, and the same value as Lucy's `LUCY_MEMORY_API_TOKEN`.

The JWKS URL and the issuer are different strings on purpose. The URL is where this
container reaches keyring; the issuer is the string keyring **mints its tokens with**, and
it has to match keyring's own `KEYRING_ISSUER` character for character or every token is
refused.

## Configuration

Every knob is an environment variable prefixed `MEMORY_`. **A `MEMORY_`-prefixed variable
that matches no setting is a startup error**, naming every offender at once, so
`MEMORY_AUDEINCE=memory-api` fails loudly instead of leaving the audience on its default
with nothing in the logs to say so.

### Core

| Variable | Default | What it does |
| --- | --- | --- |
| `MEMORY_APP_NAME` | `memory-api` | The service's own name. Nothing reads it today; it exists for family parity. |
| `MEMORY_ENVIRONMENT` | `local` | Reported by both probes. |
| `MEMORY_LOG_LEVEL` | `INFO` | Passed to uvicorn by the `memory-api` entry point. |
| `MEMORY_LOG_FORMAT` | `json` | `json` or `console`. Accepted and validated for family parity; this service has no structured-logging pipeline yet, so it currently changes nothing. |
| `MEMORY_HOST` | `127.0.0.1` | |
| `MEMORY_PORT` | `8009` | The family allocation. |
| `MEMORY_DATABASE_PATH` | `var/memory.db` | The one file every memory lives in. Parent directories are created at startup. `:memory:` is honoured as SQLite's in-memory sentinel, which is what the tests use — it is a string rather than a path for exactly that reason. |

### Identity

This service verifies keyring's tokens locally and never calls keyring at request time.

| Variable | Default | What it does |
| --- | --- | --- |
| `MEMORY_KEYRING_JWKS_URL` | `http://127.0.0.1:8001/.well-known/jwks.json` | Where keyring publishes its public keys. Must be reachable from this process. |
| `MEMORY_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Pinned against the token's `iss`. Must equal keyring's `KEYRING_ISSUER` exactly, or every token is refused, identically and unhelpfully. |
| `MEMORY_AUDIENCE` | `memory-api` | Pinned against the token's `aud`, exactly. Must be non-empty, carry no surrounding whitespace and contain **no dot** — a dotted audience is an audience *family*, and this service has no compartments. Anything else is a startup error. |
| `MEMORY_JWKS_CACHE_SECONDS` | `3600` | How long keys are held before being read again. |
| `MEMORY_JWKS_MIN_REFETCH_SECONDS` | `30` | Floor between the refetches an unknown key id may provoke. Not a tuning knob: without it a stream of tokens with random `kid`s is an outbound-fetch amplifier pointed at keyring. |
| `MEMORY_KEYRING_TIMEOUT_SECONDS` | `5` | Per fetch of the key document. |

### Sibling access

| Variable | Default | What it does |
| --- | --- | --- |
| `MEMORY_SERVICE_TOKENS` | empty | JSON object of sibling name to token, `{"lucy-api": "..."}`. Each token is at least 32 characters with no surrounding whitespace, and no two services may share one; anything else is a startup error. The name is what `asserted_by` records on a memory that sibling writes. Empty refuses every `/v1/internal` call. Never a person token. |

### Background passes

| Variable | Default | What it does |
| --- | --- | --- |
| `MEMORY_FORGET_GRACE_SECONDS` | `2592000` (30 days) | How long a forgotten memory can still be restored before it is erased for good. Must be above zero. |
| `MEMORY_SWEEP_INTERVAL_SECONDS` | `3600` | How often erasure runs. `0` (or less) turns it off, which is a choice an operator may need to make; it is not a default. |
| `MEMORY_CONSOLIDATE_IDLE_SECONDS` | `2592000` (30 days) | How long a memory must go unused (last access, not creation) before the idle-merge pass may fold it into a topic summary. Must be above zero. |
| `MEMORY_CONSOLIDATE_INTERVAL_SECONDS` | `3600` | How often that pass runs. `0` (or less) turns it off. |

### settings-api

| Variable | Default | What it does |
| --- | --- | --- |
| `MEMORY_SETTINGS_API_BASE_URL` | unset | Where settings-api is. Empty means off. |
| `MEMORY_SETTINGS_API_TOKEN` | unset | This service's entry in settings-api's `SETTINGS_API_SERVICES`: at least 32 characters, never echoed on failure. Set both or neither; half a pair is a startup error. |

Off by default, and off means everything is kept exactly as before. When on,
`create_memory`, `internal_create_memory` and `reconcile_memories` ask settings-api for
their caller's `memory` namespace, presenting the same user token keyring minted, and only
after this service has verified it. Nothing else asks. The grant there
needs `audience_prefix` equal to `MEMORY_AUDIENCE` (`memory-api` unless you changed it).

**`memory.write_importance_floor`** is applied. A new memory whose `importance` is below
the person's floor is a 422 `below-importance-floor` and is not stored; in a batch, one
`ADD` below it refuses the whole batch and names the decisions that were the reason. A
correction (`correct_memory`, or a batch `UPDATE`) is not held to it: it replaces something
already remembered, and refusing it would leave what it corrects standing. Nor is a write
that repeats a memory already remembered: it revises that memory's importance, expiry and
confidence and stores nothing new, and refusing it would keep more about the person rather
than less. A write that names no importance carries 5. The catalogue says this setting **refuses** rather than
falls back, so when the floor cannot be known — settings-api has never answered, is down
with nothing cached for that person, or answered with a value that is not a floor — the
write is a 503 `preferences-unavailable` and nothing is stored. A settings-api that has no
such key cannot have been told a floor, and everything is kept. If settings-api refuses
this service (401/403), a write that adds a memory is a 503 with a fixed body that names
neither the grant nor the URL. settings-api is asked only when a write would add a memory,
so a refusal, an outage or a slow answer never touches a read, a correction, a repeat, a
forget or a batch that adds no new memory.

settings-api answers a person who never chose with the catalogue's default, and this
service cannot tell that apart from a choice. Keeping everything for them needs that
default to be `1`; a settings-api whose catalogue still says `3` holds everybody who never
chose to a floor of three. settings-api's `GET /v1/settings/schema` shows which it has.

**`memory.consolidation`** is in the catalogue and does nothing here. The idle-merge pass
runs in the background on `MEMORY_CONSOLIDATE_*`, for every account, and has no person's
token to show settings-api, so it cannot read anybody's choice when it runs. And memory-api
has no session-end signal, so `on_session_end` has nothing to mean. Honouring it needs the
caller that knows a session ended to say so; until then, setting it stores a value and
changes nothing. `memory.retrieval_limit` and `memory.retrieval_trust_floor` are applied by
the LUCY hub, which makes the recall, and are not read here either.

Nothing is fetched at startup.

### Deliberate absences

There is no setting that disables token verification, none that lets one account read
another's memories, and none that turns off the credential refusal.

## The database file, and what is in it

One SQLite file in WAL mode, with `-wal` and `-shm` sidecars beside it.

**Nothing in it is encrypted.** keyring encrypts its credential material, so a
world-readable file there leaks metadata. Here a world-readable file leaks *everything*: a
person's own sentences about their own life, the assistant's inferences about them, and the
topic titles that say what subjects exist at all. The event log will tell you when
something was remembered even after the text is gone.

It is **never committed**. `var/`, `*.db`, `*.db-wal` and `*.db-shm` are all in
`.gitignore`, and the comment there says why.

The service creates the parent directory if it is missing, and every time it opens the
database it sets the file and its `-wal` and `-shm` sidecars to `0600`. That chmod is
skipped silently where the filesystem does not support it (Windows, some network and
bind-mounted filesystems), and the directory it creates takes the process umask, so own the
directory as well and check both:

```bash
install -d -m 700 /var/lib/memory
stat -c '%a %n' /var/lib/memory /var/lib/memory/memory.db*
```

Put it somewhere nothing serves from: never inside a static directory, a backup directory
another process syncs, or a container volume shared with anything else.

## Backups

```bash
sqlite3 var/memory.db "VACUUM INTO '/backups/memory-$(date -u +%Y%m%dT%H%M%SZ).db'"
chmod 600 /backups/memory-*.db
```

**`VACUUM INTO`, not `cp`.** A plain copy of a live WAL database can miss committed
transactions that are still in the write-ahead log, which produces a backup that restores
cleanly and is quietly missing the last few minutes.

**`chmod` the result.** `VACUUM INTO` creates the destination with the process umask, not
with the source's mode, and the backup is exactly as sensitive as the original.

Restoring is the reverse, with the service stopped: put the file at
`MEMORY_DATABASE_PATH` and remove any stale `-wal` and `-shm` sidecars beside it, because
they belong to the database that was there before.

Restoring a backup **resurrects memories somebody forgot**, if the forget happened after
the backup was taken. That is the one thing to think about before restoring rather than
after: keep backups exactly as long as the deal with the person says, and no longer.

## Erasure

No *memory* is deleted on request — blocks are the exception, and go immediately. The path
has four steps, and each exists because the one before it is not enough:

1. **Forget.** `forget_memory`, `delete_memory` or `forget_all_memories` stamps
   `forgotten_at`. The memory vanishes from both views immediately, and from the topic
   index with it. (`forget_all_memories` also removes the account's blocks outright; those
   have no tombstone and do not come back.)
2. **The grace period.** `restore_memory` undoes a forget for as long as the row is still
   there, which is the difference between a person changing their mind and a person losing
   something. The clock starts at the *first* forget: forgetting twice does not extend it.
3. **The sweep.** `SQLStore.sweep(grace_seconds)` deletes every row forgotten longer ago
   than that, with its search-index entry and its links, and records an `erase` event
   holding the id and nothing else.
4. **The WAL truncate.** `PRAGMA wal_checkpoint(TRUNCATE)` immediately after, so erased
   text does not survive in checkpointed journal pages. With `secure_delete=ON` the freed
   pages are overwritten rather than merely unlinked. Without step 4, "erased" means "not in
   the table", which is not what anybody asking for erasure meant.

**The service sweeps itself.** A background task runs every
`MEMORY_SWEEP_INTERVAL_SECONDS` for the life of the process and erases everything past
`MEMORY_FORGET_GRACE_SECONDS`. It sleeps before its first pass, so a process that is
crash-looping cannot turn restarts into faster deletion, and a pass that fails — a locked
database, a disk briefly full — does not stop the next one.

Two things to know when running more than one process against one database. Set
`MEMORY_SWEEP_INTERVAL_SECONDS=0` on all but one of them: the work is idempotent, so
duplicate sweeping is wasteful rather than wrong, but there is no reason to pay for it. And
every pass logs one line: `sweep_completed erased=N` at INFO, or `sweep_failed
error_type=…` at WARNING, so if you depend on the timing of an erasure, alert on the
failures and on the completions stopping.

Whatever interval you choose, state it to the people whose memories these are. A grace
period nobody can name is not a grace period.

**There is no operator path into somebody's memories**, and that is the point. If an account
is gone from keyring and nobody can mint a token for it, its rows are unreachable through
the API and the remaining option is SQL on the box, by whoever runs it.

## Consolidation

A topic that keeps collecting facts nobody has needed in a month is ranking noise. A
background pass, on the same shape as the sweeper, groups current trusted facts,
procedures and summaries in one topic whose `last_accessed_at` is older than
`MEMORY_CONSOLIDATE_IDLE_SECONDS`. Groups of two or more become one `kind=summary` row
(`source=consolidation`, `asserted_by=memory-api`); the originals are superseded, not
deleted, so the audit view still has them.

Only memories in the same compartment merge: the same scope, profile and session. A
profile's topic also holds each of its sessions' memories, and folding those together would
move a fact into a compartment that could not see it before, which is what a correction is
refused for. The summary is written with `trust=inferred` in that compartment, its body is
the members' bodies joined with `; ` (clamped to 16000 characters), and it takes the title
of the oldest member and the highest importance among them. The topic's line in the index is
rewritten from it. Because it is `inferred`, a search with `include_inferred=false` leaves
it out.

It sleeps before its first pass, and like the sweeper a failed pass does not stop the next
one. Every pass logs `consolidation_completed summaries=N` at INFO or `consolidation_failed
error_type=…` at WARNING. A window of zero idle seconds is refused by the store: rewriting
everything the moment it is written is not consolidation. Untrusted memories are never
merged, confirmed or not. A combined body that looks like a credential is skipped rather
than stored. With more than one process against one database, set
`MEMORY_CONSOLIDATE_INTERVAL_SECONDS=0` on all but one of them, as with the sweep.

## Health

```json
{
  "status": "degraded",
  "version": "0.1.0",
  "environment": "production",
  "uptime_seconds": 1204.5,
  "checks": {
    "keyring":  {"status": "degraded",
                 "detail": {"reachable": false,
                            "reason": "keyring's signing keys could not be fetched"}},
    "database": {"status": "ok", "detail": {"reachable": true, "reason": null}}
  }
}
```

| Probe | Answers | Point it at |
| --- | --- | --- |
| `GET /healthy` | That the process is running. No I/O, never fails. | Container healthchecks and orchestrator **liveness** — anything whose reaction is a restart. |
| `GET /ready` | keyring's signing keys and the database, checked on every call. 503 when either is unusable. | Load balancers and **readiness** gates — anything whose reaction is to take the instance out of rotation. |

Getting this the wrong way round is expensive in one specific direction: a restart policy
pointed at `/ready` restarts the container every time keyring is slow, for a problem a
restart cannot fix and that is not this service's.

The database check is a real read against a real table, not `SELECT 1` — the latter answers
from the connection alone and would keep saying yes after the file underneath it had been
deleted or its permissions changed. Its `reason` is the exception's **type name** and never
its message, because `/ready` is unauthenticated and a sqlite error routinely carries the
path of the database.

The keyring check asks keyring itself whenever it holds no fresh keys, so a newly started
process reports keyring's real state rather than waiting for a token to find out. One case
is easy to misread: keys from a successful fetch keep verifying tokens through an outage,
and that is reported as `"status": "ok"` with `"reachable": false` and the reason
`keyring could not be reached; tokens are verified against cached keys`. The instance still
works — taking it out of rotation would turn keyring's outage into this service's — but an
operator can see it before the grace runs out.

## Logs

Logging is the standard library's, at `MEMORY_LOG_LEVEL`, with uvicorn's access log; the
`memory-api` entry point sends this service's own records to uvicorn's handler. It writes
these records of its own, each carrying an **exception type name**, a **count**, a status
code or a setting's *name*, never an exception's arguments or a setting's value — a
`ValueError` raised deep in a write path routinely carries the value, and here the value is
a sentence somebody wrote about their own life:

| Record | Level | When |
| --- | --- | --- |
| `unhandled_exception error_type=…` | ERROR | A request failed with a 500. The `request_id` in the body ties the caller's report to it. |
| `sweep_completed erased=N` | INFO | A sweep pass finished. |
| `sweep_failed error_type=…` | WARNING | A sweep pass failed; the next one still runs. |
| `consolidation_completed summaries=N` | INFO | A consolidation pass finished. |
| `consolidation_failed error_type=…` | WARNING | A consolidation pass failed; the next one still runs. |
| `per_person_settings_off` / `per_person_settings_on namespace=memory` | INFO | At startup: whether settings-api is in use. |
| `settings_unavailable namespace=memory` | WARNING | settings-api has never answered this process; a write that adds a memory is a 503. |
| `settings_stale namespace=memory` | INFO | settings-api is down and a person's cached settings were used. |
| `settings_refused namespace=memory key=write_importance_floor` | WARNING | settings-api is down and the floor must not be guessed; the write is a 503. |
| `settings_rejected namespace=memory status_code=N` | WARNING | settings-api refused this service. Check the grant. |
| `setting_unusable namespace=memory key=write_importance_floor` | WARNING | settings-api answered with something that is not a floor; the write is a 503. |

Nothing else is logged, which is why there is no redaction processor to configure: there is
no path by which a memory's text reaches a log line.

## What each failure means

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every request 401 | `MEMORY_KEYRING_ISSUER` or `MEMORY_AUDIENCE` disagrees with what keyring minted | Make the issuer match keyring's `KEYRING_ISSUER` exactly, and mint with `{"audience": "<MEMORY_AUDIENCE>"}`. |
| Every request 503 with `Retry-After` | keyring's key document cannot be fetched | Check `MEMORY_KEYRING_JWKS_URL` is reachable from this host. `/ready` says which dependency. |
| Every `/v1/internal` call 401 while person-facing calls work | The calling service's token is not in `MEMORY_SERVICE_TOKENS` (empty refuses everyone), or `X-Keyring-User-Token` is missing or not a memory-api token | Add the sibling's token to `MEMORY_SERVICE_TOKENS` and restart; send the person's own token in the header. The refusal is identical for every cause, by design. |
| Startup error naming `MEMORY_*` variables | A typo under the prefix | The message names every offender at once. |
| Startup error about the audience | `MEMORY_AUDIENCE` is empty, padded with whitespace, or contains a dot | Give it a plain name. |
| `unable to open database file` | The directory is not writable by the service user | The parent directory is created at startup; it still has to be creatable. Check ownership of the volume. |
| `/ready` reports the database degraded | The file was moved, deleted, or its permissions changed under a live connection | The `reason` is the exception type. Restart after fixing the path. |
| A write refused with `below-importance-floor` | The memory's `importance` is below the floor its owner chose in settings-api | Working as intended. Send it with a higher importance only if it really matters more; the person can lower their floor. |
| Writes 503 `preferences-unavailable` while reads work | settings-api is unreachable and the importance floor is never guessed, or it refused this service | `settings_rejected` in the log means a grant: give memory-api the `memory` namespace with `audience_prefix` equal to `MEMORY_AUDIENCE`. Otherwise bring settings-api back, or unset `MEMORY_SETTINGS_API_BASE_URL` to keep everything for everyone. |
| A legitimate memory refused with `credential-refused` | It contains a long high-entropy run, or a credential-looking prefix | There is no override, by design. Store a description of the thing instead, and put the thing in keyring. |
| A search returns nothing for a word that is plainly in a memory | The memory is untrusted and unconfirmed, expired, superseded, forgotten, not yet valid, in another profile, or an `episode` outside its session | Check with `list_memories?include_forgotten=true&include_history=true`, which hides none of those. |
