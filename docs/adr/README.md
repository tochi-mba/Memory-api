# ADRs

Decisions specific to memory-api belong here, one file each, numbered in order. Family
decisions — the Python floor, the port allocation, where the hub lives — stay in the
meta-repo's [docs/adr](https://github.com/tochi-mba/LUCY-assistant/tree/main/docs/adr).

The reasoning behind this service's load-bearing choices is currently written where it is
enforced rather than in separate records: the module docstrings in
`src/memory_api/domain/topics.py` and `src/memory_api/store/sql.py`, and the invariants in
[AGENTS.md](../../AGENTS.md). Any of those that later needs to be argued with rather than
simply read should graduate to a numbered ADR here.
