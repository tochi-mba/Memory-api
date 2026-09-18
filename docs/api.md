# The HTTP API

Twenty-one person-facing operations, plus ten sibling-facing `internal_*` operations. The
person-facing set is shaped as tool calls. The internal set is not: a model is never given
those names.

`operation_id`s are **public contract**: they become MCP tool names, so renaming one breaks
every client with a tool bound to it. `tests/test_contract.py` pins the exact set as a set,
not as a minimum, so an addition is as visible as a rename.

Interactive docs at `/docs` on a running instance; the machine-readable contract is
`/openapi.json`.

## Authenticating

Every route except `GET /healthy` and `GET /ready` needs a keyring token:

```
Authorization: Bearer <RS256 JWT>
```

The audience must be exactly `MEMORY_AUDIENCE` (`memory-api` unless the deployment changed
it). memory-api verifies the token **locally** against keyring's published keys and never
calls keyring at request time.

The token's `sub` is the only identity this service gets. **No route and no request body
can name an account**: a cross-account read is not refused by a check, it is inexpressible,
because no handler has anywhere to put another person's id. A contract test walks the
generated schema asserting that no path segment and no parameter is called `account_id`,
`account`, `user_id`, `subject`, `sub` or `asserted_by`.

Two failures worth telling apart:

| | Meaning |
| --- | --- |
| **401** | The token was not accepted. One message — `token refused` — for every reason: expired, wrong audience, wrong issuer, forged, malformed, or signed with a key keyring does not publish. You learn nothing from which, deliberately. |
| **503** | keyring could not be reached to fetch the verifying keys. **Your token is probably fine.** The response carries `Retry-After: 5`; retry, do not re-authenticate. |

## The operations

| Method | Path | operation_id | Success |
| --- | --- | --- | --- |
| GET | `/healthy` | `check_liveness` | 200 |
| GET | `/ready` | `check_readiness` | 200 / 503 |
| GET | `/v1/whoami` | `whoami` | 200 |
| POST | `/v1/memory` | `create_memory` | 201 |
| GET | `/v1/memory` | `list_memories` | 200 |
| DELETE | `/v1/memory` | `forget_all_memories` | 200 |
| GET | `/v1/memory/search` | `search_memories` | 200 |
| POST | `/v1/memory/batch` | `reconcile_memories` | 200 |
| GET | `/v1/memory/blocks` | `list_memory_blocks` | 200 |
| GET | `/v1/memory/blocks/{label}` | `get_memory_block` | 200 |
| PUT | `/v1/memory/blocks/{label}` | `write_memory_block` | 200 |
| DELETE | `/v1/memory/blocks/{label}` | `delete_memory_block` | 204 |
| GET | `/v1/memory/topics` | `list_memory_topics` | 200 |
| GET | `/v1/memory/topics/{topic_id}` | `get_memory_topic` | 200 |
| PATCH | `/v1/memory/topics/{topic_id}` | `update_memory_topic` | 200 |
| GET | `/v1/memory/{memory_id}` | `get_memory` | 200 |
| POST | `/v1/memory/{memory_id}/correct` | `correct_memory` | 201 |
| POST | `/v1/memory/{memory_id}/confirm` | `confirm_memory` | 200 |
| POST | `/v1/memory/{memory_id}/forget` | `forget_memory` | 200 |
| POST | `/v1/memory/{memory_id}/restore` | `restore_memory` | 200 |
| DELETE | `/v1/memory/{memory_id}` | `delete_memory` | 204 |

These ten are the sibling-service surface. They take two credentials and a model is never
given them. The account still comes from the person's token.

| Method | Path | operation_id | Success |
| --- | --- | --- | --- |
| POST | `/v1/internal/memory` | `internal_create_memory` | 201 |
| GET | `/v1/internal/memory` | `internal_list_memories` | 200 |
| GET | `/v1/internal/memory/search` | `internal_search_memories` | 200 |
| GET | `/v1/internal/memory/blocks` | `internal_list_memory_blocks` | 200 |
| GET | `/v1/internal/memory/topics` | `internal_list_memory_topics` | 200 |
| GET | `/v1/internal/memory/topics/{topic_id}` | `internal_get_memory_topic` | 200 |
| GET | `/v1/internal/memory/{memory_id}` | `internal_get_memory` | 200 |
| POST | `/v1/internal/memory/{memory_id}/correct` | `internal_correct_memory` | 201 |
| POST | `/v1/internal/memory/{memory_id}/confirm` | `internal_confirm_memory` | 200 |
| POST | `/v1/internal/memory/{memory_id}/forget` | `internal_forget_memory` | 200 |

## Authenticating as a sibling

`/v1/internal/*` is for another service in the family, typically Lucy, not for a person
and not for a model. It takes **two** credentials:

```
Authorization: Bearer <this service's MEMORY_SERVICE_TOKENS entry>
X-Keyring-User-Token: <the person's memory-api JWT>
```

The person's token still has `aud=memory-api`. The account still comes from that token's
`sub`. The service token only proves the caller is a sibling this deployment is willing
to talk to. Empty `MEMORY_SERVICE_TOKENS` refuses every internal call. A stolen person
token without the service credential cannot use this path; a service cannot name an
account.

`asserted_by` on a memory written here is the configured service name, not the person.
That is provenance: the person authorised the call, the service made it.

## The two views of the same rows

`list_memories` and `search_memories` read the same table and are deliberately separate
routes rather than one route with a flag, because the difference is a security boundary and
a boolean is too easy to get wrong in a hurry.

**`list_memories` is the audit view.** Everything held, with its provenance: where it came
from, who asserted it, what it superseded, when it stops being true. It includes untrusted
claims on purpose. This is what you show a person who asks what is remembered about them.

**`search_memories` is the retrieval view.** This is what goes into a prompt, and six
filters apply that the audit view does not:

| Excluded | Why |
| --- | --- |
| Untrusted memories | Their content came from somebody who is not this person. They return only after `confirm_memory`. |
| Forgotten memories | `include_forgotten` is ignored here. |
| Superseded versions | A correction wins over what it corrected. `include_history` is ignored here. |
| Expired memories | `expires_at` in the past. |
| Anything but `fact`, `procedure` and `summary` | Unless the memory is session-scoped and belongs to the `session_id` you asked with. A `summary` is what consolidation writes and is retrieved like a fact. An `episode` is not: it is a thing that happened, not a thing that is the case. |
| Other profiles and other sessions | Retrieval always narrows to account-wide memories plus the named `profile`'s. With no `profile` parameter you get account-scope memories only. |

Results come back **ranked**, not ordered, and retrieval **writes**: every memory it
returns has its `last_accessed_at` stamped and its `access_count` incremented, which is
what feeds the recency term of the next search. See
[docs/architecture.md](architecture.md) for the ranking formula.

## Writing a memory

```http
POST /v1/memory
{"title": "Home city", "body": "Moved to Bristol", "kind": "fact", "importance": 7}
```

| Field | Default | Bounds |
| --- | --- | --- |
| `title` | required | 1–200 characters |
| `body` | `""` | ≤ 16000 characters |
| `value` | `null` | any JSON |
| `kind` | `fact` | `episode` \| `fact` \| `procedure` \| `summary` |
| `scope` | `account` | `account` \| `profile` \| `session` |
| `profile` | `null` | 1–200 characters; **required** for `profile` and `session`, **refused** for `account` |
| `session_id` | `null` | 1–200 characters; **required** for `session`, **refused** for anything else |
| `source` | `person` | ≤ 1000 characters |
| `trust` | `stated` | `stated` \| `observed` \| `inferred` \| `untrusted` |
| `confidence` | `1.0` | 0–1; breaks ranking ties, nothing else |
| `importance` | `5` | 1–10 |
| `occurred_at` | `null` | epoch seconds — recorded and returned, never filtered on |
| `valid_from` | now | epoch seconds |
| `expires_at` | `null` | epoch seconds; must be after `valid_from` when both are given |

Bodies are `extra="forbid"`: an invented field is a 422, not a silent ignore. That is what
stops an `account_id` or an `asserted_by` in a body from looking like it worked.

**`asserted_by` is server-derived** — the subject of the token keyring signed — and
`source` is a claim the caller makes about where the memory came from. Nothing verifies
`source` and nothing could. They are never conflated.

**Writing the same thing twice returns the memory that already exists.** Two writes are the
same when scope, profile, session, kind, title, body, value and *trust* all match and the
existing memory is current and not forgotten. Trust is part of that key on purpose: an
untrusted claim from a web page must not be deduplicated into the person's own stated fact
and inherit its trust. The response is still 201, carrying the original memory's id.

**Credential-shaped content is refused** with a 422 that names keyring and never echoes
what was sent. This applies to the whole request, structured `value` included, and to
blocks and topic rewrites. There is no setting that disables it.

## Corrections, and why never delete-then-add

`correct_memory` writes a new memory and retires the old one, linked:

```http
POST /v1/memory/mem_ab…/correct
{"title": "Home city", "body": "Moved to Bristol", "valid_from": 1767225600}
```

The retired memory keeps its row and gains `valid_to` (the new memory's `valid_from`) and
`superseded_by_id`; the new one carries `supersedes_id` and **inherits the old one's
topic**, so a fact is never separated from its own history. `valid_from` defaults to now.

This is what makes `?as_of=` answerable, and it is why delete-then-add is wrong: it throws
away the fact that something used to be true, and a question about last year then has no
answer at all.

A correction is a **409** when:

- the target is already superseded — a chain has one head, and correcting the middle of it
  would produce two current answers to one question;
- the target has been forgotten;
- the correction changes `scope`, `profile` or `session_id`, which would quietly move a
  fact out of the compartment that could see it;
- `valid_from` is earlier than the memory it replaces, because the two versions would
  overlap and `as_of` would have two answers for one instant.

A correction naming a memory that is not yours is a 404, like every other id that is not
yours.

## Trust, and confirming

`untrusted` is for anything a third party said — a web page, an email, a document. It is
stored and listed like anything else and **never retrieved**, so it cannot reach a prompt.
`confirm_memory` promotes it to `stated` and stamps `confirmed_at`.

Confirm only what the person themselves confirmed. Confirming on their behalf defeats the
entire point of the trust level; see [docs/mcp.md](mcp.md).

## Forgetting, restoring and erasing

| Operation | What it does |
| --- | --- |
| `forget_memory` | Hides the memory from both views and schedules it for erasure. Forgetting something already forgotten returns it unchanged and keeps the **first** moment it was forgotten, so a retry cannot extend the grace period. |
| `delete_memory` | The same operation, spelled the way a REST client expects. 204. |
| `restore_memory` | Undoes a forget, until the sweeper erases the row. Restoring something that was never forgotten succeeds and changes nothing. |
| `forget_all_memories` | Marks every memory forgotten and removes every block. Returns `{"forgotten": n}` — how many rows were still remembered — and not the memories themselves, because handing back what was just erased would be a copy of it. |

No **memory** is deleted by any of these; erasure is a separate, later step, and
[docs/operations.md](operations.md#erasure) has the grace period and the sweeper. **Blocks
are the exception**: `forget_all_memories` removes them outright, as does
`delete_memory_block`. There is no tombstone and no undo for a block.

## Reconciling a batch

`reconcile_memories` takes the output of an extraction pass and applies it **atomically** —
either every decision lands or none does, so a failure halfway through cannot leave a
correction stored without the thing it corrected being retired.

```http
POST /v1/memory/batch
{"decisions": [
  {"action": "ADD",    "memory": {"title": "Favourite tea", "body": "Earl Grey"}},
  {"action": "UPDATE", "memory_id": "mem_ab…",
                     "memory": {"title": "Home city", "body": "Bristol"}},
  {"action": "DELETE", "memory_id": "mem_cd…"},
  {"action": "NOOP",   "memory_id": "mem_ef…"}
]}
```

One to a hundred decisions. `ADD` and `UPDATE` require a `memory`; `UPDATE`, `DELETE` and
`NOOP` require a `memory_id`; anything else is a 422. `UPDATE` is a correction, `DELETE` is
a forget.

`data[i]` is the memory decision `i` produced, in order — which is the only way a caller
can tell which of its NOOPs was actually a NOOP. Send NOOP for what you decided to leave
alone: it costs one row read and makes the result a complete account of what the pass
considered rather than only of what it changed.

## The topic index

Memories cluster into named subjects. The index is one line per subject, and it is the
thing an assistant carries in context every turn — forty lines instead of four hundred —
expanding a topic only once it has decided that topic is the one it needs.

```http
GET /v1/memory/topics?profile=work&limit=50
```

```json
{"data": [{"id": "top_ab…", "account_id": "acct_7f…", "profile": null,
           "key": "city home", "title": "Home city",
           "summary": "Moved to Bristol", "kind": "fact",
           "memory_count": 3, "unconfirmed": 1, "importance": 7,
           "first_seen": 1767225600.0, "last_seen": 1767312000.0,
           "last_summarised_at": null, "revision": 1}],
 "has_more": true, "total": 47}
```

**`total` is how many topics exist, not how many came back**, so a renderer can say
"showing 12 of 47" and a caller can always tell "that is all of them" from "that is the
first page". A caller that cannot tell the difference will quietly reason from a fraction.

`memory_count`, `unconfirmed`, `last_seen` and `importance` are recomputed from the
memories that are in the topic *right now*, never stored counters. `unconfirmed` counts
untrusted members; they are **not** in `memory_count` and never reach retrieval.

A topic whose memories have all been forgotten or superseded disappears from the index
rather than lingering as a title with nothing behind it, and **a topic made entirely of
untrusted memories never appears at all** — its title was written from content somebody
else supplied, and the title is the part that reaches the prompt.

`GET /v1/memory/topics/{topic_id}` expands one: the topic plus its memories, most recently
used first, untrusted members left out, `limit` 1–100 (default 50). A topic with nothing
current behind it answers 404.

`PATCH /v1/memory/topics/{topic_id}` is what a consolidation pass writes back: `title`,
`summary`, or both. Sending neither is a 422. **Membership is not editable from here**, and
that is the safeguard — a bad summarising pass can make the index read poorly, but it can
never quietly move a fact into another subject.

A title is flattened to one line and clamped to 80 characters, a summary to 200. Both are
rendered into a structured block a model reads, where a newline would break the shape and a
title long enough to fill the budget would push out the topics underneath it.

`profile` narrows the index to that profile's topics plus the account-wide ones. **With no `profile` you get the account-wide topics only** — a profile's subjects are its own, which is what keeps a work summary out of a personal one.

## Filters

`list_memories` and `search_memories` take the same query parameters, because both are the
`Selection` model the store already reads. It forbids extra fields, so a misspelled
`?includ_forgotten=true` is a 422 rather than a filter that silently did not apply — which
on this surface is the difference between "these are all your memories" and "these are the
ones that got through a typo".

| Parameter | Default | What it does |
| --- | --- | --- |
| `profile` | — | Narrow to one profile; account-scope memories are always included. |
| `session_id` | — | With `profile`, admits that session's memories. |
| `limit` | `20` | 1–100. |
| `order` | `asc` | `asc` \| `desc`, over write order. |
| `after` / `before` | — | Cursors. See below. |
| `include_forgotten` | `false` | Audit view only. |
| `include_history` | `false` | Superseded versions. Audit view only. |
| `include_inferred` | `true` | Set false to drop `trust: inferred`. Both views. |
| `include_stated` | `true` | Set false to drop `trust: stated`. Both views. |
| `as_of` | now | Epoch seconds. What was true at that instant. |
| `q` | — | ≤ 1000 characters. Full-text query. |

`as_of` is how you answer a question about a time before a correction was made. In the
audit view it is what `include_history=false` measures the validity window against; in the
retrieval view it also decides what counts as expired.

A memory written with a `valid_from` in the future is stored immediately and appears in
neither view until that moment arrives.

## Pagination

Cursors, not offsets. `after` and `before` are **memory ids**, and they compare on the row's
insertion sequence, so `after` means "written after this one, in the order you asked for"
and reverses meaning with `order=desc`. A cursor naming a memory that is not yours is a 404,
the same as any other id that is not yours.

```json
{"data": [ … ], "has_more": true,
 "first_id": "mem_ab…", "last_id": "mem_cd…"}
```

`first_id` and `last_id` are the ends of the page you were given, and both are `null` on an
empty page rather than invented. Walk forward by passing the previous `last_id` as `after`.

**Cursors are for `list_memories`.** `search_memories` returns a ranked page, and a cursor
filters by write order rather than by rank, so paging a search does not mean what it looks
like it means. Ask for a larger `limit` instead; `has_more` still tells you honestly that
the ranked set was longer than the page.

## Search

`?q=` runs against an FTS5 index over title, body and serialised value. Every term is
quoted before it reaches the engine, so FTS operators and punctuation are **data, never a
query language**: `concise OR`, `NEAR(`, `*` and an unbalanced quote are all words to look
for, not a syntax error and not a 500. Terms are OR-ed.

A query that is empty or only whitespace is a **422**, because "no matches" and "you did
not ask for anything" are different answers. A query of punctuation quotes to a phrase that
tokenises to nothing and honestly matches nothing, which is the same distinction made one
step later.

Without `q`, `search_memories` still ranks: it returns the most relevant memories for the
current profile and session by recency of use and importance alone.

`list_memories` accepts `q` too. There it filters and nothing more — no ranking, no access
stamp — so the page stays in write order and stays pageable by cursor.

## Memory blocks

Blocks are the small, hand-maintained sections an assistant carries every turn — who the
person is, who the assistant is meant to be. They are not searched, not ranked and not
clustered; they are always there, which is why each one has a character limit.

```http
PUT /v1/memory/blocks/human
{"body": "Prefers short answers. Lives in Bristol.", "char_limit": 2000}
```

`label` is 1–64 characters and is the whole address. `body` is at most 24000 characters and
must fit `char_limit` (1–24000, default 16000) — a body longer than its own limit is
**refused rather than truncated**, because silently cutting the end off a block loses
whatever the person put at the bottom of it. A write replaces the block wholesale; there is
no partial edit, because a block is small enough to rewrite and a patch language would be a
second thing to get wrong.

Deleting a block that is not there **succeeds**. A delete that answered 404 would make a
retry after a dropped connection look like a failure. Reading one that is not there is a
404.

## Errors

Every failure is RFC 9457 `application/problem+json`, from all four sources — a domain
rule, FastAPI's validation, Starlette's router, and a bug — so a client, or a model calling
this as a tool, has exactly one error shape to parse.

```json
{
  "type": "https://memory-api.invalid/problems/not-found",
  "title": "Not found",
  "status": 404,
  "detail": "Memory not found for this account.",
  "request_id": "5c1f9f0f7f2f4e6c8a1b2c3d4e5f6a7b"
}
```

Validation failures add `errors`, a list of `{"location", "message"}`. **`detail` and
`errors` never echo the offending value.** That is not politeness: on this service the thing
that failed validation is a sentence somebody wrote about their own life, or the credential
the secret check just refused, and a 422 body is logged by the caller, shown in a transcript
and often handed straight back to a model. There is a test that sends a sentinel value in a
failing request and asserts it appears nowhere in the response.

| Status | `type` slug | When |
| --- | --- | --- |
| 401 | `unauthorized` | The token was missing or not accepted. Same body every time. |
| 404 | `not-found` | No such memory, block or topic — **identical** to the answer for one belonging to another account. There is no 403 in this service: a 403 would confirm the id exists, which is the one fact that must not cross between accounts. |
| 409 | `conflict` | A correction the store cannot apply: already superseded, forgotten, moved between scopes, or starting too early. |
| 422 | `validation-failed` | The request body or query string broke a rule. Carries `errors`. |
| 422 | `invalid-memory` | A search query with no words in it. |
| 422 | `credential-refused` | Credential-shaped content. The message names keyring. |
| 500 | `internal-server-error` | A bug. The detail is withheld deliberately — quote the `request_id`. |
| 503 | `keyring-unreachable` | keyring's signing keys could not be fetched. Carries `Retry-After: 5`. Not your token. |

Every response — success or failure — carries `X-Request-ID`. Send your own and it is
honoured, capped at 64 characters, so one trace can span services; send none and one is
generated. The same value is the `request_id` in a problem body, and it is the only thing a
500 gives you, because the log record on this side has the rest.

## Probes

| Operation | Route | What it answers |
| --- | --- | --- |
| `check_liveness` | `GET /healthy` | That the process is running. No I/O, never fails, no token. |
| `check_readiness` | `GET /ready` | keyring's signing keys **and** the database, one line each. 503 when either is unusable. No token. |

Liveness deliberately reports nothing about dependencies: an orchestrator restarts a
container when liveness fails, and a service that failed liveness during keyring's outage
would be restarted repeatedly for somebody else's problem.
[docs/operations.md](operations.md#health) has the response shape and which probe to point
what at.
