"""Which sibling is calling, proved by a static service token.

The internal surface takes two credentials, and this module is the first of them:

* ``Authorization: Bearer <service token>`` proves *which service* is asking.
* ``X-Keyring-User-Token`` proves *who* it is asking for. That is the verifier, and the
  account id comes from that token's ``sub`` and from nowhere else.

Both are required. One header meaning either thing would make it possible to send only one
and have it mean whichever was convenient. A deployment with no tokens configured refuses
every internal call, which is the right behaviour for a store nobody has been told to
trust yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from keyring_client import AuthenticationError as ServiceRefusedError
from keyring_client import ServiceAuthenticator as KeyringServiceAuthenticator

from memory_api.auth.verifier import TOKEN_REFUSED, AuthenticationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["ServiceAuthenticator", "ServiceCaller"]


@dataclass(frozen=True, slots=True)
class ServiceCaller:
    """A registered service acting for one verified person."""

    account_id: str
    audience: str
    service: str


class ServiceAuthenticator:
    """Turns a presented service token into the name it was configured for."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = KeyringServiceAuthenticator(dict(tokens))

    @property
    def configured(self) -> tuple[str, ...]:
        return self._tokens.configured

    def identify(self, token: str) -> str:
        """Return the configured service name, or refuse.

        Raises:
            AuthenticationError: the token matches no configured service.
        """
        try:
            return self._tokens.identify(token)
        except ServiceRefusedError as exc:
            raise AuthenticationError(TOKEN_REFUSED) from exc
