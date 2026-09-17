"""How memories cluster into topics, and why they must.

A flat list of remembered facts cannot be summarised, and a model handed twenty unrelated
sentences cannot tell what it knows *about*. So a memory belongs to a **topic**: a named
cluster with a title, a one-line summary and a count. What an assistant carries in its
context every turn is then the topic *index*, not the contents -- forty lines instead of
four hundred -- and it expands one topic only when it has decided that topic is the one it
needs.

That inversion is what makes an always-current memory affordable. Without it you choose
between carrying everything, which is expensive and buries the useful facts, and carrying
nothing, which leaves the model unable to know that it knows anything at all.

## Why the matching is deliberately simple

Assignment is exact-key first, then a token-overlap similarity, then a new topic. It is not
an embedding, and that is a decision rather than a shortcut:

* It is **explainable**. When a memory lands in the wrong topic a person can see exactly
  why, because the rule is "these titles share these words". An embedding's answer to the
  same question is a number.
* It is **deterministic**. The same memories in the same order always produce the same
  topics, which is what makes the behaviour testable at all.
* It has **no model dependency**, so writing a memory never waits on a network call, and a
  provider outage cannot stop the person being remembered.

An embedding backend is a strictly better matcher and can replace `similarity` without
touching anything else, which is why the seam is one function.
"""

from __future__ import annotations

import re

WORDS = re.compile(r"[a-z0-9]+")

SIMILARITY_THRESHOLD = 0.5
"""Half the words shared. Chosen to be wrong in the safe direction: a threshold that is too
high scatters one subject over several topics, which is untidy; one that is too low merges
two subjects into a topic whose summary is true of neither, which is misleading."""

MAX_TITLE = 80
MAX_SUMMARY = 200

# The words that make two unrelated titles look related. "The home address" and "the home
# page" share two words out of three, and only one of them is a word about anything.
STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "s",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "you",
        "your",
    }
)


def words(text: str) -> tuple[str, ...]:
    """The meaningful words of a title, lowercased and in order.

    Stopwords are dropped, but only when something survives them: a memory titled "how to"
    still needs a key, and an empty key would collapse every such memory into one topic.
    """
    found = tuple(WORDS.findall(text.lower()))
    meaningful = tuple(word for word in found if word not in STOPWORDS)
    return meaningful or found


def key_for(title: str) -> str:
    """The stable handle a topic is looked up by: its meaningful words, sorted.

    Sorted, because "tea preferences" and "preferences: tea" are the same subject, and a
    person writing the second one should not get a second topic.
    """
    return " ".join(sorted(set(words(title))))


def similarity(left: str, right: str) -> float:
    """How alike two keys are, between 0 and 1.

    Jaccard over the word sets: shared words divided by all words. Two keys with nothing in
    common score zero, identical keys score one, and the threshold sits at half.
    """
    first, second = set(left.split()), set(right.split())
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def choose[T](key: str, candidates: dict[str, T]) -> T | None:
    """Which existing topic a new memory joins, or nothing when it starts its own.

    Exact key first, because an exact match is not a judgement call. Otherwise the closest
    candidate above the threshold, with ties broken by the key itself so that two runs over
    the same data agree -- an assignment that depends on dictionary order is an assignment
    that changes when nothing changed.
    """
    if key in candidates:
        return candidates[key]
    best: tuple[float, str] | None = None
    for candidate in candidates:
        score = similarity(key, candidate)
        if score >= SIMILARITY_THRESHOLD and (
            best is None or (-score, candidate) < (-best[0], best[1])
        ):
            best = (score, candidate)
    return candidates[best[1]] if best else None


def summarise(title: str, body: str) -> str:
    """A placeholder summary, good enough until something better writes one.

    The first sentence of the body is almost always the fact itself, and the title is the
    honest fallback when there is no body. A model writes a better summary during
    consolidation; this exists so that the index is never blank in the meantime, because a
    blank summary is indistinguishable from a topic about nothing.
    """
    first = body.strip().split(".")[0].strip() if body.strip() else ""
    return clamp(first or title.strip(), MAX_SUMMARY)


def clamp(text: str, limit: int) -> str:
    """One line, no longer than the limit.

    Titles and summaries are rendered into a structured block that a model reads. A newline
    in one of them would break the block's shape, and a title long enough to fill the
    budget would push out the topics underneath it -- so both are flattened here, at the
    boundary, rather than trusted to be well behaved.
    """
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1].rstrip() + "…"
