"""Application configuration.

Every knob is an environment variable prefixed ``MEMORY_``. Unknown variables under
the prefix are rejected rather than ignored.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from keyring_client import check_service_token
from pydantic import AfterValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "MEMORY_"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


def _validated_service_token(value: SecretStr | None) -> SecretStr | None:
    """Refuse a token settings-api would never accept, without echoing it.

    Runs after wrapping as ``SecretStr``, so a validation error's input is the secret
    (asterisks), not the presented string.
    """
    if value is not None:
        check_service_token(value.get_secret_value())
    return value


ServiceToken = Annotated[SecretStr | None, AfterValidator(_validated_service_token)]

# The health routes `api/routers/health.py` serves. The meta-repo's parity check looks for
# these literals in this repository's source, and finds them in the router as well as here.
ROUTES = ("/healthy", "/ready")


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = "memory-api"
    environment: str = "local"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    host: str = "127.0.0.1"
    port: PositiveInt = 8009

    database_path: str = "var/memory.db"
    """The one file every memory lives in.

    A ``str`` and not a ``Path``, unlike the sibling services, because ``:memory:`` is a
    sqlite sentinel rather than a filename: expanding and resolving it -- which is what a
    ``Path`` field would invite -- turns an in-memory database into a file literally called
    ``:memory:`` in the working directory, and the tests that rely on isolation would
    quietly start sharing one.
    """

    forget_grace_seconds: PositiveFloat = 30 * 86_400.0
    """How long a forgotten memory can still be restored before it is erased for good.

    Long enough that somebody who forgot the wrong thing has a month to notice, short
    enough that "forget this" means something. It is the operator's call, not the person's:
    a per-memory grace period would be one more thing to get wrong in the moment somebody
    is trying to delete something.
    """

    sweep_interval_seconds: float = 3_600.0
    """How often to erase what is past its grace period. Zero turns sweeping off.

    Off is a legitimate choice -- a deployment may erase on its own schedule, and one that
    runs two processes against one database wants exactly one of them doing it -- but it
    has to be chosen, because the route descriptions promise that erasure happens.
    """

    consolidate_idle_seconds: PositiveFloat = 30 * 86_400.0
    """How long a memory must go unused before it may be merged into a topic summary.

    Recency is last access, not creation: a fact retrieved yesterday is still live even if
    it was written a year ago. Zero idle would rewrite everything the moment it was
    written, which is noise rather than consolidation, and the store refuses that window.
    """

    consolidate_interval_seconds: float = 3_600.0
    """How often the idle-merge pass runs. Zero turns it off."""

    service_tokens: dict[str, str] = Field(default_factory=dict)
    """Sibling service tokens admitted to ``/v1/internal``. Empty refuses every caller.

    JSON object, ``{"lucy-api": "<32+ chars>"}``. The person's token still binds the
    account; these only prove which service is asking.
    """

    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    keyring_issuer: str = "http://127.0.0.1:8001"
    audience: str = "memory-api"
    jwks_cache_seconds: PositiveFloat = 3_600.0
    jwks_min_refetch_seconds: PositiveFloat = 30.0
    keyring_timeout_seconds: PositiveFloat = 5.0

    # -- Per-person settings -----------------------------------------------------------
    settings_api_base_url: str | None = None
    """Where settings-api is. Unset, every person gets this service as it stands.

    Set, a write reads its owner's ``memory.write_importance_floor`` and refuses a new
    memory below it. Nothing else in the ``memory`` namespace is read here: see
    :mod:`memory_api.core.preferences` for which entries other services read and which
    this one cannot honour yet.
    """

    settings_api_token: ServiceToken = None
    """This service's entry in settings-api's ``SETTINGS_API_SERVICES``.

    At least 32 characters, the rule settings-api enforces on its side. Its grant there
    needs ``audience_prefix`` equal to ``MEMORY_AUDIENCE`` (``memory-api`` unless the
    operator changed it): settings-api is shown the same user token keyring minted.
    """

    @property
    def settings_api(self) -> tuple[str, SecretStr] | None:
        """Where settings-api is and how to authenticate to it, or ``None`` when unused.

        One value rather than two optional ones, so nothing downstream has to re-establish
        that the pair is whole: :meth:`_check_settings_api_is_whole` already refused to
        construct settings where it is not.
        """
        if self.settings_api_base_url is None or self.settings_api_token is None:
            return None
        return self.settings_api_base_url, self.settings_api_token

    @field_validator("settings_api_base_url")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        """``MEMORY_SETTINGS_API_BASE_URL=`` in a ``.env`` means off, not an empty URL."""
        return value or None

    @model_validator(mode="after")
    def _check_settings_api_is_whole(self) -> Self:
        """Refuse half a settings-api configuration.

        A URL with no token would be refused on every call, and a token with no URL is a
        secret configured for nothing. Either is somebody's mistake, and startup is the
        cheapest place to hear about it.
        """
        if (self.settings_api_base_url is None) != (self.settings_api_token is None):
            msg = "settings_api_base_url and settings_api_token must be set together"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _audience_is_usable(self) -> Self:
        value = self.audience
        if not value or value.strip() != value or "." in value:
            msg = "MEMORY_AUDIENCE must be non-empty, trimmed, and contain no dot"
            raise ValueError(msg)
        return self

    @field_validator("service_tokens", mode="before")
    @classmethod
    def _parse_service_tokens(cls, value: object) -> dict[str, str]:
        if value is None or value == "":
            return {}
        parsed: object
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                message = "MEMORY_SERVICE_TOKENS must be a JSON object of name to token"
                raise ValueError(message) from exc
        elif isinstance(value, dict):
            parsed = value
        else:
            message = "MEMORY_SERVICE_TOKENS must be a JSON object of name to token"
            raise ValueError(message)  # noqa: TRY004
        if not isinstance(parsed, dict):
            message = "MEMORY_SERVICE_TOKENS must be a JSON object of name to token"
            raise ValueError(message)  # noqa: TRY004
        tokens: dict[str, str] = {}
        for name, token in parsed.items():
            if not isinstance(name, str) or not name or not isinstance(token, str):
                message = "MEMORY_SERVICE_TOKENS keys and values must be non-empty strings"
                raise ValueError(message)
            check_service_token(token)
            tokens[name] = token
        if len(set(tokens.values())) != len(tokens):
            message = "two services share a service token; each needs its own"
            raise ValueError(message)
        return tokens


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Refuse unknown ``MEMORY_*`` variables so a typo fails at startup."""
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields}
    source = environ if environ is not None else os.environ
    unknown = sorted(key for key in source if key.startswith(ENV_PREFIX) and key not in known)
    if unknown:
        msg = "unknown environment variables: " + ", ".join(unknown)
        raise RuntimeError(msg)


def load_settings() -> Settings:
    """Load settings and refuse unknown env vars under the prefix."""
    check_for_unknown_env_vars()
    return Settings()
