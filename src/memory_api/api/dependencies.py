"""FastAPI dependency wiring.

Two things here are load-bearing rather than plumbing.

**Identity is a dependency, not a parameter.** ``CurrentCallerDep`` is the only way a route
learns whose memories it is touching, so "read somebody else's memories" is not forbidden by
a check somewhere -- it is inexpressible, because no route has anywhere to put the other
person's id.

**The selection model is reused, not restated.** ``SelectionDep`` hands FastAPI the same
:class:`~memory_api.domain.models.Selection` the store already takes. Writing the fourteen
query parameters out by hand would work exactly once: the first time somebody adds a field
to ``Selection``, the HTTP surface would silently stop offering it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from memory_api.auth.services import ServiceCaller
from memory_api.auth.verifier import TOKEN_REFUSED, AuthenticationError, VerifiedCaller
from memory_api.core.container import Container
from memory_api.domain.models import Selection
from memory_api.store.worker import StoreWorker

bearer_scheme = HTTPBearer(auto_error=False)

MISSING_CREDENTIALS = "a keyring token is required"
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_store(container: ContainerDep) -> StoreWorker:
    return container.store


StoreDep = Annotated[StoreWorker, Depends(get_store)]


async def get_current_caller(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VerifiedCaller:
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)
    return await container.verifier.verify(credentials.credentials)


CurrentCallerDep = Annotated[VerifiedCaller, Depends(get_current_caller)]


async def get_service_caller(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    user_token: Annotated[str | None, Header(alias=USER_TOKEN_HEADER)] = None,
) -> ServiceCaller:
    """A registered service acting for the person named by the second credential.

    The account still comes from the person's token. The service token only proves the
    caller is a sibling this deployment is willing to talk to, so a stolen person token
    cannot use this path and a service cannot name who it is asking about.
    """
    if credentials is None:
        raise AuthenticationError(TOKEN_REFUSED)
    service = container.services.identify(credentials.credentials)
    if user_token is None or not user_token.strip():
        raise AuthenticationError(TOKEN_REFUSED)
    person = await container.verifier.verify(user_token)
    return ServiceCaller(account_id=person.account_id, audience=person.audience, service=service)


ServiceCallerDep = Annotated[ServiceCaller, Depends(get_service_caller)]

SelectionDep = Annotated[Selection, Query()]
"""Every list and search filter, as query parameters, from the model the store reads.

``Selection`` forbids extra fields, so a misspelled ``?includ_forgotten=true`` is a 422
rather than a filter that silently did not apply -- which on this surface is the difference
between "these are all your memories" and "these are the ones that got through a typo"."""


def asserted_by(caller: VerifiedCaller | ServiceCaller) -> str:
    """Who the service *knows* made this assertion.

    Not to be confused with ``source``, which travels in the request body and is a claim:
    the caller saying where it believes a memory came from. This is the other thing -- the
    subject of a signature keyring checked. A caller can write ``source: "the doctor"``; it
    cannot write who it is.

    On the person-facing surface this is their account id. On the internal surface it is
    the configured service name: the person authorised the call, the service made it.
    """
    return caller.account_id if not isinstance(caller, ServiceCaller) else caller.service
