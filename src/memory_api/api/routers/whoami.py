"""One authenticated route: echo the verified caller.

Kept even though the service now has a real surface, because it is the cheapest way for a
client to answer "is my token good, and who does this service think I am" without writing
anything. It is also the only route whose response *is* the identity, which makes it the
natural place to notice if token verification ever starts believing the wrong subject.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status

from memory_api.api.dependencies import CurrentCallerDep
from memory_api.api.schemas import Problem, WhoAmIResponse

router = APIRouter(prefix="/v1", tags=["whoami"])

_PROBLEM: dict[str, Any] = {"model": Problem}


@router.get(
    "/whoami",
    operation_id="whoami",
    summary="Who this service believes you are",
    response_model=WhoAmIResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
    description=(
        "Returns the account id and audience taken from the verified Bearer token, and "
        "nothing else. Use it to check a token without writing anything."
    ),
)
async def whoami(caller: CurrentCallerDep) -> WhoAmIResponse:
    """Return the account id and audience from the verified Bearer token."""
    return WhoAmIResponse(account_id=caller.account_id, audience=caller.audience)
