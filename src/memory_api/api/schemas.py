"""Wire models: the shapes this service puts on the network.

Separate from :mod:`memory_api.domain.models` on purpose. Those are what the store reads
and writes; these are the envelopes around them, and the two are free to move apart -- the
HTTP contract is public, its operation ids are MCP tool names, and it must be able to grow
a field without the storage layer having an opinion about it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from memory_api.domain.models import Block, Memory

PROBLEM_CONTENT_TYPE = "application/problem+json"


class LivenessResponse(BaseModel):
    status: str = Field(description="Always 'alive'.")
    version: str
    environment: str
    uptime_seconds: float


class CheckResult(BaseModel):
    status: str
    detail: dict[str, object] = Field(default_factory=dict)


class ReadyResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float
    checks: dict[str, CheckResult]


class WhoAmIResponse(BaseModel):
    account_id: str
    audience: str


class FieldError(BaseModel):
    """One field-level validation failure."""

    location: str = Field(description="Dotted path to the offending field.")
    message: str = Field(description="What is wrong with it.")


class Problem(BaseModel):
    """An error, in the shape RFC 9457 defines.

    Every failure this service produces uses it, so a client -- or a model calling this as a
    tool -- has exactly one error shape to understand.

    ``detail`` names the rule that failed and **never echoes the offending value**. That is
    not politeness. On this service the thing that failed validation is a sentence somebody
    wrote about their own life, or the credential a secret check just refused, and a 422
    body is logged by the caller, shown in a transcript, and often handed back to a model.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "https://memory-api.invalid/problems/not-found",
                    "title": "Not found",
                    "status": 404,
                    "detail": "Memory not found for this account.",
                    "request_id": "5c1f9f0f7f2f4e6c8a1b2c3d4e5f6a7b",
                }
            ]
        }
    )

    type: str = Field(description="A URI identifying the problem kind.")
    title: str = Field(description="Short, human-readable summary of the problem kind.")
    status: int = Field(description="The HTTP status code.")
    detail: str = Field(description="Explanation specific to this occurrence.")
    request_id: str | None = Field(
        default=None,
        description="Correlates this response with the server logs for the same request.",
    )
    errors: list[FieldError] | None = Field(
        default=None,
        description="Per-field detail, present only for validation failures.",
    )


class BlockList(BaseModel):
    """Every block this account has.

    An object rather than a bare array, because a top-level JSON array cannot grow a
    sibling field later without breaking every client that parsed it.
    """

    data: list[Block]


class BatchResult(BaseModel):
    """What a reconciled batch did, in the order it was asked to do it.

    Position matters: ``data[i]`` is the memory that decision ``i`` produced, which is the
    only way a caller can tell which of its NOOPs was actually a NOOP.
    """

    data: list[Memory]


class ForgetAllResult(BaseModel):
    """How much was forgotten.

    A count, not the memories: handing back what was just erased would be a copy of it.
    """

    forgotten: int = Field(description="How many memories were still remembered.")
