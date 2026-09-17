# Testing

`make check` is the gate, and it is exactly four things in this order:

| Step | What runs |
| --- | --- |
| `lint` | `ruff format --check .` then `ruff check .` — no fixes, so CI and a developer see the same answer. |
| `type` | `mypy`, strict, with `warn_unreachable`, over **`src` and `tests`**. |
| `imports` | `lint-imports`: routers may not import `keyring_client`, `httpx` or `jwt`. |
| `test` | `pytest --cov --cov-report=term-missing`, branch coverage, `fail_under = 100`. |

`tests` is on mypy's path as well as in its files, because the eval suite imports
`conftest` the way pytest makes it importable; without it mypy cannot see a module the
tests certainly can.

## 100% branch coverage, and what it is for

The gate is not a target to hit by adding tests for getters. It is a tripwire: a branch
nobody has exercised is a branch nobody has thought about, and this service is one people
trust with what they have said about their own lives.

**There is no `# pragma: no cover` anywhere in `src/`**, and the family parity check fails a
repository that grows one, so a branch that cannot be reached is removed rather than
excused. The only omission is `__main__.py`, which is a `uvicorn.run` call and nothing else.

`filterwarnings = ["error"]` turns every warning into a failure. A `DeprecationWarning` from
a dependency is how you find out about a breaking change while there is still time to act
on it, and a warning that merely scrolls past is not how anybody finds out.

## Fixtures, and what they are for

`asyncio_mode = "auto"`, so an async test needs no marker.

**`client`** drives the real app over ASGI, through `LifespanManager` so the container is
built and closed exactly as it is in production, with `keyring_client.testing`'s
`FakeKeyring` behind an `httpx.MockTransport`. It signs real RS256 tokens with a real key,
serves a real JWKS document, and refuses a token minted for another audience exactly as
keyring would. No network, and no `unittest.mock` anywhere in the suite.

**`store`** holds a `SQLStore` directly. Behaviour like cursor paging, the ranking blend and
the sweep is easier to pin one call at a time than through fifteen requests — and the store
tests pass an account id explicitly, which is precisely what the HTTP layer never does.
That is the seam being tested: the store will answer for any account, and it is the absence
of any way to *say* an account over HTTP that keeps one person's memories out of another's.

**`FakeClock`** is time the test moves by hand. Temporal reasoning is most of what this
service does — what was true then, what is true now, what a correction replaced — and none
of it is testable against a clock that advances on its own. Sub-second real time also makes
decay and recency ties unstable.

Every test gets its own `:memory:` database, created and destroyed with its worker thread.
A shared file would make the suite order-dependent in the way that is hardest to see: a test
that passes alone and fails after another one wrote a memory.

## The suites

| File | What it pins |
| --- | --- |
| `test_memory_eval.py` | The quality contract. Its own section, below. |
| `test_contract.py` | The exact set of twenty-one `operation_id`s; that every operation has a summary and a longer description; that every failure a route can produce is declared; that every failure body is a `Problem`; that every request body forbids extra fields; that no path or parameter names an account; and that the literal routes are declared before `/{memory_id}`. |
| `test_memory_routes.py` | The surface rather than the storage: which literal paths survive sitting under `/{memory_id}`, that identity comes from the token and nowhere else, that the two list views really are two views, blocks, batches, forgetting everything, and that every route requires a token. |
| `test_store.py` | The store one call at a time: deduplication, the several ways a correction is refused, transitions, batch atomicity, what a listing includes, cursor paging, search, retrieval ranking, blocks, the sweep, and that an older database gains the column it lacks. |
| `test_topics.py` | Which topic a memory lands in, what the index is allowed to say, the untrusted-topic boundary, rewriting a topic's words, and the topic routes over HTTP. |
| `test_domain.py` | The pure rules: a memory that must agree with itself, the credential refusal, topic keys, similarity, choice and summaries. |
| `test_errors.py` | One error shape from all four sources — a domain rule, FastAPI's validation, Starlette's router, and a bug — plus the request id and the two things a body must never contain. |
| `test_worker.py` | The thread the database lives on. |
| `test_config.py`, `test_health.py`, `test_whoami.py` | Unknown `MEMORY_*` refused, a bad audience refused, the probes, and the token echo. |

## `test_memory_eval.py` — the quality contract

This suite was **written before the implementation**. It is not a regression net added
afterwards; it is the specification the service was built to satisfy, and it is the file to
read first if you want to know what "working" means here.

It is deterministic and involves no model. Extraction — deciding what is worth remembering
from a conversation — belongs to the hub. What this suite tests is that **persistence
cannot undo the hub's decisions**: that what the extraction pass decided is still true
tomorrow, still attributable, and still absent when it should be absent.

Six axes, three tests:

| Axis | What must hold |
| --- | --- |
| **Knowledge updates** | A correction wins. After "Lived in London" is corrected to "Moved to Bristol", a search returns Bristol and only Bristol. |
| **Temporal reasoning** | History is still answerable. The same search with `as_of` set between the two `valid_from`s returns London. |
| **Abstention** | A query that matches nothing returns nothing, rather than everything. "No matches" is an answer, and a retrieval layer that falls back to a pile of unrelated memories is how an assistant starts confidently answering the wrong question. |
| **Account isolation** | Another account asking for the memory by its id gets 404 — the same answer as for one that never existed. |
| **Untrusted sources** | An untrusted memory is stored, is absent from retrieval, and appears only after `confirm_memory`. |
| **Semantic versus episodic** | A `fact` written in one session is retrievable; an `episode` is not. A conversation's own turns are not facts about a life. |

Each test is an axis rather than a case, which is why the file is barely sixty lines. A new *case*
for an existing axis belongs in `test_store.py` or `test_memory_routes.py`; a new **axis**
belongs here, and adding one is a claim that the service promises something it did not
promise before.

## The tests that must never be deleted

- **Isolation, on every route.** Write as one account, read as another, expect 404. It has
  to hold on every route rather than on the ones somebody remembered.
- **Untrusted memories never reach retrieval**, and a topic made entirely of them never
  reaches the index. The second one has its own test class, because the title of a topic is
  the part that goes into a prompt every turn.
- **`detail` never echoes what was sent.** A sentinel value goes into a request that fails
  validation, and the test asserts it appears nowhere in the response.
- **An unhandled exception's message never reaches the caller**, and only its type name is
  logged.
- **The worker's thread guard trips.** `check_same_thread` is left on, and the test proves
  the guard is armed by deliberately tripping it — the only way to know the discipline is
  enforced rather than merely intended.
- **The credential corpus.** Tune the heuristic so both halves pass; never by deleting an
  entry. A false positive blocks a legitimate memory and there is no override.

## Running less than everything

```bash
uv run pytest tests/test_topics.py -q          # one area
uv run pytest -k "untrusted or correction" -q  # by name
uv run pytest tests/test_memory_eval.py -q     # the quality contract alone
make cov                                       # HTML report in htmlcov/
```

A green `make check` is one interpreter's answer, and so is CI's: the family workflow gates
**3.12** only. Python 3.13 is declared supported and is not yet in the matrix, so a change
that depends on interpreter version is a change nothing here would catch.
