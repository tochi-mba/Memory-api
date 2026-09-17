"""HTTP routers, in the order Starlette will try them.

``topics`` is registered before ``memory`` because ``/v1/memory/topics`` sits underneath
``/v1/memory/{memory_id}``. FastAPI 0.141 ranks an included router's literal path ahead of
another router's parameterised one, so today this order is not what makes the topic routes
reachable -- but that ranking is a detail of a dependency, it has changed before, and the
failure it would cause is silent: every topic request answered as a lookup for a memory
called "topics". One line here costs nothing and does not depend on it.
"""

from __future__ import annotations

from memory_api.api.routers import health, memory, topics, whoami

ROUTERS = (health.router, whoami.router, topics.router, memory.router)

__all__ = ["ROUTERS"]
