"""Strict public memory schemas. Provenance identity never comes from input."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Kind = Literal["episode", "fact", "procedure", "summary"]
Trust = Literal["stated", "observed", "inferred", "untrusted"]
Scope = Literal["account", "profile", "session"]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MemoryInput(StrictModel):
    title: ShortText
    body: Annotated[str, Field(max_length=16000)] = ""
    value: JsonValue = None
    kind: Kind = "fact"
    scope: Scope = "account"
    profile: ShortText | None = None
    session_id: ShortText | None = None
    source: Annotated[str, Field(max_length=1000)] = "person"
    trust: Trust = "stated"
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    importance: Annotated[int, Field(ge=1, le=10)] = 5
    occurred_at: float | None = None
    valid_from: float | None = None
    expires_at: float | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope != "account" and self.profile is None:
            msg = "profile is required for profile and session memories"
            raise ValueError(msg)
        if self.scope == "session" and self.session_id is None:
            msg = "session_id is required for session memories"
            raise ValueError(msg)
        if self.scope == "account" and self.profile is not None:
            msg = "account memories cannot name a profile"
            raise ValueError(msg)
        if self.scope != "session" and self.session_id is not None:
            msg = "session_id belongs only to session memories"
            raise ValueError(msg)
        if (
            self.expires_at is not None
            and self.valid_from is not None
            and self.expires_at <= self.valid_from
        ):
            msg = "expires_at must follow valid_from"
            raise ValueError(msg)
        return self


class Memory(MemoryInput):
    id: str
    account_id: str
    asserted_by: str
    topic_id: str | None = None
    created_at: float
    updated_at: float
    last_accessed_at: float
    access_count: int = 0
    confirmed_at: float | None = None
    valid_to: float | None = None
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    forgotten_at: float | None = None
    revision: int = 1


class Topic(StrictModel):
    """A named cluster of memories, as it appears in the index.

    `memory_count`, `last_seen`, `importance` and `unconfirmed` are computed from the
    memories that are in the topic *right now*, never stored counters. A stored counter
    drifts the moment a memory is forgotten, superseded or erased, and an index that
    overstates what it holds is worse than no index: it sends the model looking for
    something that is not there.
    """

    id: str
    account_id: str
    profile: str | None = None
    key: str
    title: str
    summary: str
    kind: Kind = "fact"
    memory_count: int = 0
    unconfirmed: int = 0
    importance: int = 5
    first_seen: float
    last_seen: float
    last_summarised_at: float | None = None
    revision: int = 1


class TopicUpdate(StrictModel):
    """What consolidation is allowed to rewrite.

    A better title and a better summary, and nothing else. Membership is decided by the
    assignment rule, not by whoever is writing the summary, so a bad summarising pass can
    make the index read poorly but can never silently move a fact into another subject.
    """

    title: ShortText | None = None
    summary: Annotated[str, Field(max_length=200)] | None = None

    @model_validator(mode="after")
    def require_something(self) -> Self:
        if self.title is None and self.summary is None:
            msg = "a topic update must change the title, the summary, or both"
            raise ValueError(msg)
        return self


class TopicPage(StrictModel):
    """The index, and an honest count of what did not fit.

    `total` is the number of topics that exist, not the number returned. The renderer needs
    both to be able to say "showing 12 of 47", and a caller that cannot tell the difference
    between "that is all of them" and "that is the first page" will quietly reason from a
    fraction.
    """

    data: list[Topic]
    has_more: bool = False
    total: int = 0


class TopicDetail(StrictModel):
    """One topic, expanded: the reason the index is only an index."""

    topic: Topic
    memories: list[Memory]


class Page[T](StrictModel):
    data: list[T]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class Selection(StrictModel):
    profile: str | None = None
    session_id: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    order: Literal["asc", "desc"] = "asc"
    after: str | None = None
    before: str | None = None
    include_forgotten: bool = False
    include_history: bool = False
    include_inferred: bool = True
    include_stated: bool = True
    as_of: float | None = None
    q: Annotated[str, Field(max_length=1000)] | None = None


class BlockInput(StrictModel):
    body: Annotated[str, Field(max_length=24000)]
    char_limit: Annotated[int, Field(ge=1, le=24000)] = 16000

    @model_validator(mode="after")
    def check_limit(self) -> Self:
        if len(self.body) > self.char_limit:
            msg = "body exceeds char_limit; shorten it before saving"
            raise ValueError(msg)
        return self


class Block(BlockInput):
    label: str
    updated_at: float


class Decision(StrictModel):
    action: Literal["ADD", "UPDATE", "DELETE", "NOOP"]
    memory_id: str | None = None
    memory: MemoryInput | None = None

    @model_validator(mode="after")
    def require_operands(self) -> Self:
        if self.action in {"ADD", "UPDATE"} and self.memory is None:
            msg = "ADD and UPDATE require a memory"
            raise ValueError(msg)
        if self.action != "ADD" and self.memory_id is None:
            msg = "UPDATE, DELETE and NOOP require memory_id"
            raise ValueError(msg)
        return self


class Batch(StrictModel):
    decisions: Annotated[list[Decision], Field(min_length=1, max_length=100)]
