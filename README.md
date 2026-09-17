# memory-api

An assistant that forgets everything the moment a conversation ends is a search box with
better manners. This is where what it learns about a person goes, so that a fact told on
Monday is still true on Friday, can be corrected when it stops being true, and can be
shown to the person it is about.

It is a service in the [LUCY](https://github.com/tochi-mba/LUCY-assistant) family, and it
authenticates against [keyring](https://github.com/tochi-mba/Keyring-api).

## What a memory is

A remembered claim, with everything needed to judge it later: who asserted it, where it
came from, how confident the writer was, when it was true, and whether it has since been
corrected.

**Kinds.** An `episode` is something that happened. A `fact` is something that is the
case. A `procedure` is how to do something. A `summary` stands in for several of the
others. Only facts and procedures are retrieved across sessions by default, because "we
talked about tour dates on Tuesday" is rarely what you want surfacing three weeks later.

**Scopes.** `account` is everywhere, `profile` is one profile, `session` is one
conversation. A work assistant and a home assistant are different people, and their
memories are too.

**Trust.** `stated` came from the person. `observed` was seen directly. `inferred` was
worked out. `untrusted` was distilled from something anyone could have written — a web
page, a document, a tool result — and is **never retrieved until somebody confirms it**.

## Topics, and why memory is not a list

A flat list of remembered facts cannot be summarised, and twenty unrelated sentences tell
a model nothing about what it knows. So memories cluster into **topics**: a named subject
with a title, a one-line summary, a count and a last-touched time.

What an assistant carries in its context every turn is the topic *index* — forty lines
instead of four hundred — and it expands one topic only when it has decided that topic is
the one it needs. That inversion is what makes an always-current memory affordable.
Without it you choose between carrying everything, which is expensive and buries the
useful facts, and carrying nothing, which leaves the assistant unable to know that it
knows anything.

Clustering is exact key, then word overlap, then a new topic. Deliberately not an
embedding: the rule is explainable when a memory lands in the wrong place, it is
deterministic, and writing a memory never waits on a network call. An embedding backend
can replace one function without touching anything else.

## The lines this service does not cross

**A memory is data, never an instruction.** Anything an assistant read on a web page can
end up in a memory, and a memory is permanent. If a memory could say *"always do X"*, then
anything the assistant ever read could rewrite its behaviour for good. Every memory is
returned with its provenance attached, and [docs/mcp.md](docs/mcp.md) instructs the layer
above to render them as third-person reported claims. The API cannot enforce that. It can
make the honest shape the easy one.

**Untrusted memories are never auto-retrieved.** A memory store is a prompt-injection
*persistence* layer, and permanence is exactly what makes it worth attacking. Anything
distilled from tool output or a page is written as `untrusted` and stays out of retrieval
until it is confirmed. A topic made entirely of unconfirmed memories never appears in the
index either, because its title came from the untrusted content.

**No secrets.** People will paste an API key into "remember this". Credential-shaped
input is refused at write time — known prefixes, private key headers, JWT shapes, and any
long high-entropy string — with a message naming keyring. The failure never echoes what
was submitted. There is no setting that turns this off.

**Nothing is hard-deleted by a correction.** *"Actually, I moved in March"* writes a new
memory and closes the old one with `valid_to` and `superseded_by_id`. That is what makes
*"why do you think that?"* answerable, makes temporal queries work, and makes a correction
reversible. Real erasure is a separate, deliberate path with a grace period and a sweeper.

**Isolation comes from storage, not from prompting.** Every query carries a mandatory
`account_id` predicate taken from the verified token. There is no admin API and no
endpoint that reads somebody's memories on their behalf.

## Retrieval

Ranking combines relevance (FTS5/bm25), recency and importance, min-max normalised before
they are combined so that two incomparable scales are never added together.

Recency decays from **last access, not creation**. That distinction matters more than it
looks: decaying from creation makes a stable, frequently used fact like a home timezone
look old and evicts it, while yesterday's one-off survives.

## Getting started

```bash
make install     # create the venv and install everything
make check       # the gate: lint, strict types, contracts, 100% branch coverage
make run         # serve on :8009, docs at /docs
```

You will need a running keyring to get a token. See
[docs/operations.md](docs/operations.md) for every setting, and
[docs/api.md](docs/api.md) for the routes.

## Where things are

| | |
| --- | --- |
| The routes, and the `operation_id`s that become tool names | [docs/api.md](docs/api.md) |
| Storage, ranking, topics and the worker thread | [docs/architecture.md](docs/architecture.md) |
| Running it, every `MEMORY_*` setting, backup and erasure | [docs/operations.md](docs/operations.md) |
| The suites, and the quality contract written before the code | [docs/testing.md](docs/testing.md) |
| How a model-facing layer must frame what it reads | [docs/mcp.md](docs/mcp.md) |
| Service-level decisions | [docs/adr/README.md](docs/adr/README.md) |

`make check` is the same four gates as everywhere else in the family: lint, types,
import contracts, and tests at 100% branch coverage.
