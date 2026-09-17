"""Application configuration.

Every knob is an environment variable prefixed ``MEMORY_``. Unknown variables under
the prefix are rejected rather than ignored.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, model_validator
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
