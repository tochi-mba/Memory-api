# Contributing

`make check` is the gate: lint, strict types, import contracts, and tests at 100% branch
coverage. Run it before you commit, and never pipe it to `head`.

The family standard this service holds to — Makefile verbs, the CI caller, health routes,
configuration rules, RFC 9457 errors, the no-pragma rule — lives in the meta-repo's
[CONTRIBUTING.md](https://github.com/tochi-mba/LUCY-assistant/blob/main/CONTRIBUTING.md).
`python scripts/parity.py` there scores this repository against it.

Read [AGENTS.md](AGENTS.md) before editing. The invariants listed in it are each held up
by a test; if you find yourself changing one of those tests, you are changing behaviour
and it belongs in the changelog.

Commits are conventional (`feat(scope):`, `fix(scope):`, `chore:`), imperative, and
explain why rather than restating the diff.
