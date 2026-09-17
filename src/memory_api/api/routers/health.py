"""Liveness and readiness.

The split is the whole point. ``/healthy`` says the process is running and does no I/O, so
an orchestrator never restarts a process because a dependency was slow. ``/ready`` says it
can actually serve, which for this service means two things: keyring's signing keys can be
fetched, and the database answers. Either one failing makes every ``/v1`` route useless, so
either one failing takes the instance out of rotation rather than letting it accept traffic
it will only fail.

Neither probe is authenticated, which is why the database check reports an exception's type
name and never its message: a sqlite error routinely carries the path of the file.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from memory_api import __version__
from memory_api.api.dependencies import ContainerDep
from memory_api.api.schemas import CheckResult, LivenessResponse, ReadyResponse

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


@router.get(
    "/healthy",
    operation_id="check_liveness",
    summary="Whether the process is running",
    response_model=LivenessResponse,
    description=(
        "Does no I/O and never fails. Answering means the process is alive; it says nothing "
        "about whether it can serve a request, which is what `/ready` is for."
    ),
)
async def check_liveness(container: ContainerDep) -> LivenessResponse:
    """Liveness only: no I/O, never fails."""
    return LivenessResponse(
        status="alive",
        version=__version__,
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
    )


@router.get(
    "/ready",
    operation_id="check_readiness",
    summary="Whether the process can serve a request",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
    description=(
        "Checks both dependencies every call: keyring's signing keys, and the database. "
        "Answers 503 when either is unusable, because a request that needs one of them "
        "would fail anyway and failing at the load balancer is cheaper than failing at the "
        "route."
    ),
)
async def check_readiness(container: ContainerDep, response: Response) -> ReadyResponse:
    """Report whether keyring's keys can be fetched and the database answers."""
    keys_usable, keys_reason = await container.jwks.healthy()
    database_usable, database_reason = await container.store.healthy()
    checks = {
        "keyring": CheckResult(
            status=STATUS_OK if keys_usable else STATUS_DEGRADED,
            detail={"reachable": keys_reason is None, "reason": keys_reason},
        ),
        "database": CheckResult(
            status=STATUS_OK if database_usable else STATUS_DEGRADED,
            detail={"reachable": database_reason is None, "reason": database_reason},
        ),
    }
    healthy = all(check.status == STATUS_OK for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status=STATUS_OK if healthy else STATUS_DEGRADED,
        version=__version__,
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
        checks=checks,
    )
