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
| Network, outbound | keyring's JWKS URL, and nothing else |
| Secrets | **none** |

That last row is worth pausing on. memory-api holds no signing key, no admin token and no
third-party credential. It verifies somebody else's signatures with a public key it fetches
over HTTP, and it refuses to store anything that looks like a credential. What it does hold
is a person's own words, which is a different kind of sensitive and is what the rest of this
page is mostly about.

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
against `keyring_client.testing` and never touches the network. With one running:

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
`KEYRING_SERVICE_TOKENS`: that list only admits services to keyring's internal endpoints,
which this service never calls.

## In compose

```bash
make docker       # builds memory-api:local
docker run -p 8009:8009 --env-file .env \
  -v memory-data:/var/lib/memory memory-api:local
```

The image runs as a non-root user, bakes `MEMORY_HOST=0.0.0.0`, `MEMORY_PORT=8009` and
`MEMORY_LOG_FORMAT=json`, and its `HEALTHCHECK` calls `/healthy`. The keyring issuer and
JWKS URL are deliberately **not** baked in: they name the keyring this deployment trusts,
and a default in the image is how a container ends up trusting the wrong one.

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
    networks: [lucy]
```

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

### Deliberate absences

There is no setting that disables token verification, none that lets one account read
another's memories, and none that turns off the credential refusal. There is also **no
setting for the erasure grace period** — see below; the sweep takes it as an argument
because nothing in this release schedules the sweep.

## The database file, and what is in it

One SQLite file in WAL mode, with `-wal` and `-shm` sidecars beside it.

**Nothing in it is encrypted.** keyring encrypts its credential material, so a
world-readable file there leaks metadata. Here a world-readable file leaks *everything*: a
person's own sentences about their own life, the assistant's inferences about them, and the
topic titles that say what subjects exist at all. The event log will tell you when
something was remembered even after the text is gone.

It is **never committed**. `var/`, `*.db`, `*.db-wal` and `*.db-shm` are all in
`.gitignore`, and the comment there says why.

The service creates the parent directory if it is missing, but it does **not** set the
file's mode — SQLite creates it with the process umask. Own the directory instead, and
check:

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

Nothing in this service hard-deletes on request. The path has four steps, and each exists
because the one before it is not enough:

1. **Forget.** `forget_memory`, `delete_memory` or `forget_all_memories` stamps
   `forgotten_at`. The memory vanishes from both views immediately, and from the topic
   index with it.
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

**This release ships no scheduler and no route that calls the sweep.** It is a store
operation, and the grace period is its argument rather than a setting, so until something
schedules it, erasure is something an operator runs — with the service stopped, because the
running process expects to be the only writer:

```bash
uv run python - <<'PY'
from memory_api.store.sql import SQLStore

store = SQLStore("var/memory.db")
print("erased:", store.sweep(grace_seconds=7 * 86400))
store.close()
PY
```

Run it on a schedule you can state to the person whose memories they are. A grace period
nobody can name is not a grace period.

**There is no operator path into somebody's memories**, and that is the point. If an account
is gone from keyring and nobody can mint a token for it, its rows are unreachable through
the API and the remaining option is SQL on the box, by whoever runs it.

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

Logging is the standard library's, at `MEMORY_LOG_LEVEL`, with uvicorn's access log. This
service writes exactly one record of its own: `unhandled_exception error_type=…`, carrying
the exception's **type name only**, never its arguments — a `ValueError` raised deep in a
write path routinely carries the value, and here the value is a sentence somebody wrote
about their own life. The `request_id` in the 500 body is what ties the caller's report to
that record.

Nothing else is logged, which is why there is no redaction processor to configure: there is
no path by which a memory's text reaches a log line.

## What each failure means

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every request 401 | `MEMORY_KEYRING_ISSUER` or `MEMORY_AUDIENCE` disagrees with what keyring minted | Make the issuer match keyring's `KEYRING_ISSUER` exactly, and mint with `{"audience": "<MEMORY_AUDIENCE>"}`. |
| Every request 503 with `Retry-After` | keyring's key document cannot be fetched | Check `MEMORY_KEYRING_JWKS_URL` is reachable from this host. `/ready` says which dependency. |
| Startup error naming `MEMORY_*` variables | A typo under the prefix | The message names every offender at once. |
| Startup error about the audience | `MEMORY_AUDIENCE` is empty, padded with whitespace, or contains a dot | Give it a plain name. |
| `unable to open database file` | The directory is not writable by the service user | The parent directory is created at startup; it still has to be creatable. Check ownership of the volume. |
| `/ready` reports the database degraded | The file was moved, deleted, or its permissions changed under a live connection | The `reason` is the exception type. Restart after fixing the path. |
| A legitimate memory refused with `credential-refused` | It contains a long high-entropy run, or a credential-looking prefix | There is no override, by design. Store a description of the thing instead, and put the thing in keyring. |
| A search returns nothing for a word that is plainly in a memory | The memory is untrusted, expired, superseded, forgotten, or is an `episode`/`summary` outside its session | Check with `list_memories`, which hides none of those. |
