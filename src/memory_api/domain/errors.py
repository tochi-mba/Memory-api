"""Safe, value-free failure vocabulary.

Every error carries the HTTP status and the problem-type slug it turns into, so the API
layer maps a failure by asking the failure rather than by consulting a table that has to be
kept in step with this file by hand.

The base class is ``MemoryFault`` and not the obvious ``MemoryError`` because Python already
has a built-in of that name. Shadowing it is not a style complaint: an ``except
MemoryError`` written anywhere in the process -- here, in a dependency, in a script somebody
pastes into a shell -- would catch whichever of the two was in scope, and the one it
silences is the one saying the interpreter has run out of memory.

No message may name the value that caused it. A refusal is read back by a model, written to
a caller's log and shown in a transcript, and the thing that failed is usually a sentence
somebody wrote about their own life.
"""

from __future__ import annotations


# N818 wants an `Error` suffix. That rule and the shadowing rule cannot both be obeyed
# here, and shadowing a builtin is the more expensive mistake, so the suffix loses.
class MemoryFault(Exception):  # noqa: N818
    """A memory operation that cannot be carried out."""

    status = 422
    code = "invalid-memory"


class NotFoundError(MemoryFault):
    """No such memory, block or topic *for this account*.

    Deliberately the same answer for "does not exist" and "belongs to somebody else". A 403
    would confirm that a memory with that id exists, which is exactly the fact that must not
    cross between accounts.
    """

    status = 404
    code = "not-found"


class ConflictError(MemoryFault):
    """The memory exists but is not in a state this operation can act on."""

    status = 409
    code = "conflict"


class SecretError(MemoryFault):
    """Credential-shaped material was offered as memory, and refused."""

    code = "credential-refused"
