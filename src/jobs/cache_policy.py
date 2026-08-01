"""Shared policy for search-time acceleration.

Search results themselves are never served from a TTL cache: every user
search starts (or joins) an upstream refresh. This policy only controls
whether downstream, profile-scoped computations may reuse cached work.
"""

from __future__ import annotations

from typing import Any

from src.core.config import load_config


def search_acceleration_policy(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the normalized compatibility policy exposed to callers and UI."""
    raw = (config if config is not None else load_config()).get("search_cache", {})
    try:
        ttl_hours = max(int(raw.get("ttl_hours", 24)), 1)
    except (TypeError, ValueError):
        ttl_hours = 24
    return {
        "enabled": bool(raw.get("enabled", True)),
        "ttl_hours": ttl_hours,
        "mode": "acceleration_only",
        "upstream_refresh": "always",
    }


__all__ = ["search_acceleration_policy"]
