"""What one person has chosen, and what memory-api does when it cannot ask.

The deployment's configuration says how memory-api behaves for everybody. settings-api
holds what each person has chosen within that, and this module is the one place the two
meet: it turns a caller's token into the importance floor a new memory is held to. Nothing
is read at startup, and with no settings-api configured every person gets the floor of one
-- everything is kept, which is exactly what memory-api did before it read anybody's
settings at all.

Four rules shape it.

**A person may narrow what is kept and never widen it.** The floor is how significant a new
memory must be before it is written down; raising it keeps less. There is no deployment
floor above one, so the person's own choice is the floor. :mod:`memory_api.domain.importance`
says which writes it applies to.

**An outage refuses rather than guesses.** ``write_importance_floor`` is ``refuse`` in the
catalogue: writing nothing during an outage is recoverable, and writing down more about a
person than they agreed to is not. So when settings-api cannot say what somebody chose --
it has never answered, or it is down and the key is refused, or it answered with a value
that is not a floor -- the floor is *unknown*, and a write that needs it is a 503. Reads,
corrections and forgets never need it and are not failed for it. A value cached from an
earlier answer is the person's own and is used.

**A refusal is not an outage.** settings-api answering 401 or 403 means this service is
misconfigured -- a missing grant, a wrong token -- and carrying on would hide that behind
behaviour that happens to work. The request fails instead.

**Only what this service can honour is read.** ``retrieval_limit`` and
``retrieval_trust_floor`` are applied by the LUCY hub, which asks for a recall. The rest of
the namespace is not read here, ``consolidation`` among it: the idle-merge pass runs in the
background with no person's token to show settings-api, and memory-api has no session-end
signal for ``on_session_end`` to mean anything. Honouring it would be faking a mechanism
this service does not have.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from settings_client import (
    HttpSettingsClient,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)

from memory_api.domain.errors import PreferencesUnavailableError
from memory_api.domain.importance import HIGHEST, LOWEST

if TYPE_CHECKING:
    from settings_client import ResolvedSettings, SettingsClient

    from memory_api.core.config import Settings

    class PreferenceSource(Protocol):
        """Where a request's preferences come from.

        Lives in the type-checking block, as the consolidator's protocol does, so the
        coverage gate is not asked to execute a Protocol body nobody ever will.
        """

        async def for_token(self, user_token: str, /) -> Preferences:
            """The preferences of whoever ``user_token`` belongs to.

            Raises:
                PreferencesUnavailableError: settings-api refused this service.
            """
            ...

        async def aclose(self) -> None:
            """Release whatever this holds open."""
            ...


logger = logging.getLogger(__name__)

NAMESPACE = "memory"
FLOOR = "write_importance_floor"

REFUSED = "settings-api did not accept this service's request for your settings"
NOT_GUESSED = (
    "your importance floor could not be read from settings-api and must not be guessed, so "
    "nothing new was remembered; try again shortly"
)


@dataclass(frozen=True, slots=True)
class Preferences:
    """One person's choices, as this service applies them to one request."""

    write_importance_floor: int | None
    """The least importance a new memory may carry, or ``None`` when it cannot be known."""

    def importance_floor(self) -> int:
        """The floor, read at the moment a write needs it.

        Raises:
            PreferencesUnavailableError: settings-api could not say, and this setting is
                never guessed.
        """
        if self.write_importance_floor is None:
            raise PreferencesUnavailableError(NOT_GUESSED)
        return self.write_importance_floor


DEPLOYMENT = Preferences(write_importance_floor=LOWEST)
"""What everybody gets when nobody's own choices are read: everything is kept."""

UNKNOWN = Preferences(write_importance_floor=None)
"""What a person gets when settings-api cannot say what they chose."""


class PersonPreferences:
    """One verified caller's choices for one request, asked for only when a write needs them.

    A request's dependency builds this from a token it has already verified, and nothing
    is asked of settings-api until :meth:`importance_floor` is awaited. A batch of
    corrections and forgets never awaits it, so a settings-api that is down, slow or
    refusing this service has no say in an operation the floor does not apply to.
    """

    def __init__(self, source: PreferenceSource, user_token: str) -> None:
        self._source = source
        self._user_token = user_token

    async def importance_floor(self) -> int:
        """The floor, asked of settings-api now.

        Raises:
            PreferencesUnavailableError: settings-api refused this service, or could not
                say and this setting is never guessed.
        """
        return (await self._source.for_token(self._user_token)).importance_floor()


class DeploymentPreferences:
    """Everybody keeps everything: what memory-api did before it read settings-api."""

    async def for_token(self, _user_token: str, /) -> Preferences:
        return DEPLOYMENT

    async def aclose(self) -> None:
        """Nothing is held open."""


class SettingsApiPreferences:
    """Each person's own floor, read from settings-api."""

    def __init__(self, *, client: SettingsClient) -> None:
        self._client = client

    async def for_token(self, user_token: str, /) -> Preferences:
        try:
            # Account-wide: the floor is one value per person, so no profile is named.
            resolved = await self._client.resolve(NAMESPACE, user_token=user_token)
        except SettingsUnavailable:
            # Never answered, so not even the catalogue's outage rule is known here. The
            # catalogue says this key refuses, and a guess is what that rule exists to stop.
            logger.warning("settings_unavailable namespace=%s", NAMESPACE)
            return UNKNOWN
        except SettingsRejected as error:
            # The status only: settings-api's own detail names grants and namespaces, which
            # an operator reads in its log rather than a caller reading it in ours.
            logger.warning(
                "settings_rejected namespace=%s status_code=%d", NAMESPACE, error.status_code
            )
            raise PreferencesUnavailableError(REFUSED) from error

        if resolved.stale:
            logger.info("settings_stale namespace=%s", NAMESPACE)
        try:
            return Preferences(write_importance_floor=_floor(resolved))
        except SettingsRefused:
            logger.warning("settings_refused namespace=%s key=%s", NAMESPACE, FLOOR)
            return UNKNOWN

    async def aclose(self) -> None:
        await self._client.aclose()


def build_preference_source(
    settings: Settings, *, client: SettingsClient | None = None
) -> PreferenceSource:
    """Choose where preferences come from, and say which in the log.

    Args:
        settings: the configuration, which says whether settings-api is in use.
        client: substituted by tests with :class:`settings_client.testing.FakeSettingsClient`,
            and used in place of building one from ``settings``.
    """
    if client is None:
        configured = settings.settings_api
        if configured is None:
            logger.info("per_person_settings_off")
            return DeploymentPreferences()
        base_url, token = configured
        client = HttpSettingsClient(base_url=base_url, service_token=token.get_secret_value())

    logger.info("per_person_settings_on namespace=%s", NAMESPACE)
    return SettingsApiPreferences(client=client)


def _floor(resolved: ResolvedSettings) -> int | None:
    """The person's floor, ``LOWEST`` when settings-api has no such key, ``None`` when unusable.

    A settings-api that predates the key cannot have been told a floor by anybody, so
    keeping everything is not a guess. A value that is not a whole number from one to ten is
    settings-api's bug, and this key is never guessed: the write waits for a real answer.
    The key is logged; the value never is.

    Raises:
        SettingsRefused: settings-api is unreachable and the key must not be guessed.
    """
    value = resolved.get(FLOOR, LOWEST)
    if isinstance(value, int) and not isinstance(value, bool) and LOWEST <= value <= HIGHEST:
        return value
    logger.warning("setting_unusable namespace=%s key=%s", NAMESPACE, FLOOR)
    return None


__all__ = [
    "DEPLOYMENT",
    "UNKNOWN",
    "DeploymentPreferences",
    "PersonPreferences",
    "Preferences",
    "SettingsApiPreferences",
    "build_preference_source",
]
