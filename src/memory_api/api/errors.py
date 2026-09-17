"""Translating exceptions into RFC 9457 problem responses.

The only place in the service that maps a failure to a status code, which is what keeps the
handlers thin: they raise domain errors and let this decide what that means over HTTP.

Three of the mappings are decisions rather than lookups.

**A memory that belongs to somebody else is 404, never 403.** A 403 confirms the id exists,
which is the one fact that must not cross between accounts -- and a memory id is exactly the
kind of thing that ends up in a URL somebody pastes. See
:class:`~memory_api.domain.errors.NotFoundError`.

**Keyring being unreachable is 503, not 401.** They are genuinely different: a 401 tells a
person to log in again, and they would be logging in again because *we* could not fetch a
public key. The ``Retry-After`` says come back rather than start over.

**An unexpected exception's text never reaches the caller.** It can carry a path, a
hostname, or the sentence somebody was trying to remember. The caller gets a request id to
quote, and the log record on this side has the rest.

The domain errors are not listed in a table here. Each one already carries its own
``status`` and ``code`` (see :mod:`memory_api.domain.errors`), so a single handler
registered against the base class serves all of them -- Starlette walks the exception's MRO
looking for one. A table would be a second place to edit every time an error is added, and
the failure mode of forgetting is a 500 for something the service understood perfectly well.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from memory_api.api.schemas import PROBLEM_CONTENT_TYPE, FieldError, Problem
from memory_api.auth.verifier import AuthenticationError, KeyringUnreachableError
from memory_api.core.context import get_request_id
from memory_api.domain.errors import MemoryFault

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

PROBLEM_BASE_URI = "https://memory-api.invalid/problems"
RETRY_AFTER_HEADER = "Retry-After"
KEYRING_RETRY_AFTER = "5"

_STATUS_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
}


# PLR0913: six keyword-only fields, because RFC 9457 has six fields. Grouping them into an
# object would add a type whose only job is to be unpacked one line later.
def problem_response(  # noqa: PLR0913
    *,
    status_code: int,
    detail: str,
    problem_type: str | None = None,
    title: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a problem+json response carrying the current request id."""
    slug = problem_type or _slug_for(status_code)
    problem = Problem(
        type=f"{PROBLEM_BASE_URI}/{slug}",
        title=title or _STATUS_TITLES.get(status_code, "Error"),
        status=status_code,
        detail=detail,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def _slug_for(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "error").lower().replace(" ", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler the app needs. Called once, by the app factory."""

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reshape FastAPI's validation errors into the one error format this API uses.

        Only the location and the message are copied. FastAPI's raw errors include the
        offending **input**, and on this service a request body is something a person said
        about their own life -- so the default handler would put a rejected memory into a
        response, a client log and quite possibly an aggregator. There is a test that sends
        a sentinel value in a request that fails validation and asserts it appears nowhere.
        """
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the request failed validation",
            problem_type="validation-failed",
            errors=[
                FieldError(
                    location=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors()
            ],
        )

    @app.exception_handler(AuthenticationError)
    async def _auth(_request: Request, exc: Exception) -> JSONResponse:
        return problem_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            problem_type="unauthorized",
        )

    @app.exception_handler(KeyringUnreachableError)
    async def _keyring_down(_request: Request, exc: Exception) -> JSONResponse:
        """Say come back, not start over.

        A 401 here would tell a person to log in again because this service could not fetch
        a public key -- sending them through keyring for a problem that is not theirs and
        that logging in would not fix.
        """
        return problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            problem_type="keyring-unreachable",
            headers={RETRY_AFTER_HEADER: KEYRING_RETRY_AFTER},
        )

    @app.exception_handler(MemoryFault)
    async def _domain(_request: Request, exc: Exception) -> JSONResponse:
        """Render a domain failure at the status and slug the error itself declares."""
        fault = exc if isinstance(exc, MemoryFault) else MemoryFault(str(exc))
        return problem_response(
            status_code=fault.status, detail=str(fault), problem_type=fault.code
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Starlette's own refusals: an unrouted path, a method a route does not have."""
        return problem_response(status_code=exc.status_code, detail=str(exc.detail))


def unhandled_problem_response(exc: BaseException) -> JSONResponse:
    """Render an unexpected exception as a 500.

    The exception's own message is withheld: it can carry filesystem paths, internal
    hostnames, or a fragment of what somebody asked to be remembered. The request id ties
    the response to the log record that does have the detail -- which is why this is invoked
    from inside :class:`~memory_api.api.middleware.RequestContextMiddleware`, while the id
    is still bound, rather than from Starlette's outermost error middleware, where the
    binding has already unwound and the response would carry no id at all.

    Only the exception's **type name** is logged, never its arguments: a ``ValueError``
    raised deep in a write path routinely carries the value in its message.
    """
    logger.error("unhandled_exception error_type=%s", type(exc).__name__)
    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="an unexpected error occurred; quote the request id when reporting it",
    )
