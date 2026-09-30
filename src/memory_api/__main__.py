"""CLI entry point."""

from __future__ import annotations

import copy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from memory_api.core.config import load_settings


def log_config(level: str) -> dict[str, Any]:
    """Uvicorn's own logging setup, plus this service's loggers on the same handler.

    Uvicorn configures only its own loggers. Without this, a record from ``memory_api`` falls
    through to Python's last-resort handler, which drops anything below WARNING -- so the
    sweeper's and the consolidator's per-pass lines would never be seen.
    """
    config = copy.deepcopy(LOGGING_CONFIG)
    config["loggers"]["memory_api"] = {
        "handlers": ["default"],
        "level": level.upper(),
        "propagate": False,
    }
    return config


def main() -> None:
    """Serve the application."""
    settings = load_settings()
    uvicorn.run(
        "memory_api.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=log_config(settings.log_level),
    )


if __name__ == "__main__":
    main()
