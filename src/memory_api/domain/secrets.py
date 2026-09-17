"""Refuse credential-shaped material before it can become durable memory."""

from __future__ import annotations

import json
import math
import re
from collections import Counter

from memory_api.domain.errors import SecretError

_PATTERNS = re.compile(
    r"(?i)bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_-]{12,}|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)
_LONG = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
ENTROPY_FLOOR = 4.5


def refuse_secrets(value: object) -> None:
    """Check structured values as well as prose; never include the input in a failure."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if _PATTERNS.search(text):
        msg = "Store credentials in keyring, then save a non-secret description here."
        raise SecretError(msg)
    for match in _LONG.finditer(text):
        token = match.group()
        probabilities = [count / len(token) for count in Counter(token).values()]
        entropy = -sum(probability * math.log2(probability) for probability in probabilities)
        if entropy > ENTROPY_FLOOR:
            msg = "High-entropy content is not memory; store credentials in keyring."
            raise SecretError(msg)
