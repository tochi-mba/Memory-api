"""Configuration rules."""

from __future__ import annotations

import os
from typing import Any

import pytest
from keyring_client.testing import ISSUER, JWKS_URL
from pydantic import ValidationError

from memory_api.auth.services import ServiceAuthenticator
from memory_api.core.config import LogFormat, Settings, check_for_unknown_env_vars, load_settings


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "audience": "memory-api",
    }
    return Settings(**{**defaults, **overrides})


def test_unknown_env_vars_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_NOT_A_REAL_SETTING", "1")
    with pytest.raises(RuntimeError, match="MEMORY_NOT_A_REAL_SETTING"):
        check_for_unknown_env_vars()


def test_bad_audience_refused() -> None:
    with pytest.raises(ValueError, match="MEMORY_AUDIENCE"):
        _settings(audience="hello.work")


def test_load_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_ENVIRONMENT", "test")
    monkeypatch.setenv("MEMORY_LOG_FORMAT", "console")
    monkeypatch.setenv("MEMORY_KEYRING_ISSUER", "https://keyring.test")
    monkeypatch.setenv("MEMORY_KEYRING_JWKS_URL", "https://keyring.test/.well-known/jwks.json")
    monkeypatch.setenv("MEMORY_AUDIENCE", "memory-api")
    for key in list(os.environ):
        if key.startswith("MEMORY_") and key not in {
            "MEMORY_ENVIRONMENT",
            "MEMORY_LOG_FORMAT",
            "MEMORY_KEYRING_ISSUER",
            "MEMORY_KEYRING_JWKS_URL",
            "MEMORY_AUDIENCE",
        }:
            monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.environment == "test"


def test_service_tokens_parse_from_json() -> None:
    token = "k" * 32
    settings = _settings(service_tokens=f'{{"lucy-api": "{token}"}}')
    assert settings.service_tokens == {"lucy-api": token}


def test_an_empty_service_tokens_value_is_no_services() -> None:
    assert _settings(service_tokens="").service_tokens == {}
    assert _settings(service_tokens=None).service_tokens == {}


def test_malformed_service_tokens_are_refused() -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        _settings(service_tokens=["nope"])
    with pytest.raises(ValidationError, match="JSON object"):
        _settings(service_tokens="[1]")
    with pytest.raises(ValidationError, match="non-empty strings"):
        _settings(service_tokens={1: "k" * 32})
    with pytest.raises(ValidationError, match="JSON object"):
        _settings(service_tokens="{")
    with pytest.raises(ValidationError, match="non-empty strings"):
        _settings(service_tokens={"lucy-api": ["nope"]})
    with pytest.raises(ValidationError, match="non-empty strings"):
        _settings(service_tokens={"": "k" * 32})
    with pytest.raises(ValidationError, match="at least"):
        _settings(service_tokens={"lucy-api": "short"})
    with pytest.raises(ValidationError, match="share"):
        _settings(service_tokens={"lucy-api": "k" * 32, "other": "k" * 32})


def test_configured_services_are_named() -> None:
    token = "k" * 32
    named = ServiceAuthenticator({"lucy-api": token})
    assert named.configured == ("lucy-api",)
