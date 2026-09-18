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
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "MEMORY_"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]

# Parity looks for these string literals in source when this tree becomes a sibling repo.
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
