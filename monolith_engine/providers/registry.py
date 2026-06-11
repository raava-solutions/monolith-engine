"""Provider registry — resolve a provider type to an adapter.

The open engine ships the single-node adapters (docker, and lxd when present).
Cloud adapters (gce) live in the closed hosted service and register the same
``ComputeProvider`` port from there.
"""

from __future__ import annotations

from monolith_engine.ports.provider import ComputeProvider
from monolith_engine.providers.docker import DockerProvider

_BUILTIN = {"docker": DockerProvider}


def get_provider(provider_type: str = "docker", **kwargs) -> ComputeProvider:
    """Return a provider adapter for the given type.

    Single-node default is docker. Unknown types raise — the hosted service
    registers its own (gce) against the same port, out of this package.
    """
    key = (provider_type or "docker").strip().lower()
    ctor = _BUILTIN.get(key)
    if ctor is None:
        raise ValueError(
            f"unknown provider type '{provider_type}'. Built-in: {', '.join(_BUILTIN)}"
        )
    return ctor(**kwargs)


__all__ = ["get_provider"]
