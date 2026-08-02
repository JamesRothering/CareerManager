"""Phase 19.4: A2 score cache write-through + read path.

The matching scorer (``src.matching.scorer``) is left untouched --- it
still returns the same :class:`ScoreBreakdown`. This module adds two
helpers around it:

* :func:`record_score` writes a :class:`JobPostingScore` row keyed by
  ``(tenant_id, snapshot_id, profile_id, profile_version, scorer_version)``;
* :func:`get_cached_score` reads the same key.

The boundary contract (D029):

* A profile edit naturally changes :func:`compute_profile_version`, so a
  new row is inserted instead of overwriting the previous one. Old
  versions stay queryable for audit but the hot read path uses only the
  current version.
* :data:`SCORER_VERSION` is bumped on hard-rule / prompt / agent
  behavior changes. The bump cold-starts cache reads.

Concurrent computes race-write the same key; the unique constraint
guarantees idempotency (one row per cache key). Conflicts are swallowed
by :func:`record_score` because by definition the existing row holds the
same verdict for the same cache key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.core.models import JobPostingScore

# Bump whenever the deterministic scorer's hard rules, weight defaults,
# or quality-multiplier logic change. The string makes "v1" / "v2"
# human-readable in audit rows.
SCORER_VERSION: str = "s1"

# The edge-case agent is a separate cache axis. ``agent_version`` lives
# on the row but defaults to this constant; bumping it cold-starts agent
# rescoring without invalidating deterministic-only scores.
AGENT_VERSION: str = "a1"


@dataclass
class CachedVerdict:
    """Subset of a cached score row the fast-path reads."""

    snapshot_id: UUID
    profile_version: str
    scorer_version: str
    verdict: str
    final_score: float | None
    score_breakdown: dict[str, Any]
    computed_at: datetime


def compute_profile_version(profile_data: dict[str, Any] | None) -> str:
    """Stable short hash of the canonical profile JSON.

    Twelve hex chars is enough to disambiguate the few profiles a user
    edits in a year while staying short enough for the UI to surface
    (``profile vXYZ`` in the review queue). Returns ``"unknown"`` for
    a missing profile so the cache key remains well-defined.
    """
    if not profile_data:
        return "unknown"
    canonical = json.dumps(profile_data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def get_cached_score(
    session: Session,
    *,
    tenant_id: str,
    snapshot_id: UUID,
    profile_id: str,
    profile_version: str,
    scorer_version: str = SCORER_VERSION,
    max_age_hours: int | None = None,
    now: datetime | None = None,
) -> CachedVerdict | None:
    """Return the cached verdict for the exact ``(tenant, snapshot,
    profile, profile_version, scorer_version)`` combination.

    A missing row is a cache miss --- the caller should run the real
    scorer and feed the result through :func:`record_score`. Older
    versions are *not* returned: D029 forbids reusing scores across
    scorer bumps, and the cache axis is intentionally narrow.
    """
    stmt = (
        select(JobPostingScore)
        .where(JobPostingScore.tenant_id == tenant_id)
        .where(JobPostingScore.snapshot_id == snapshot_id)
        .where(JobPostingScore.profile_id == profile_id)
        .where(JobPostingScore.profile_version == profile_version)
        .where(JobPostingScore.scorer_version == scorer_version)
    )
    if max_age_hours is not None:
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=max(max_age_hours, 1))
        stmt = stmt.where(JobPostingScore.computed_at >= cutoff)
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    return CachedVerdict(
        snapshot_id=row.snapshot_id,
        profile_version=row.profile_version,
        scorer_version=row.scorer_version,
        verdict=row.verdict,
        final_score=row.final_score,
        score_breakdown=dict(row.score_breakdown or {}),
        computed_at=row.computed_at,
    )


def record_score(
    session: Session,
    *,
    tenant_id: str,
    posting_id: UUID,
    snapshot_id: UUID,
    profile_id: str,
    profile_version: str,
    breakdown: Any,
    scorer_version: str = SCORER_VERSION,
    agent_version: str | None = None,
    model_id: str | None = None,
    now: datetime | None = None,
) -> JobPostingScore:
    """Insert one cache row. Idempotent under the unique key.

    Uses Postgres ``INSERT ... ON CONFLICT DO UPDATE`` so a duplicate
    key refreshes only ``computed_at`` without overwriting its audit-relevant
    verdict. This lets an expired acceleration row become reusable after a
    slow-path recompute while keeping concurrent writes transaction-safe.

    ``breakdown`` is anything with a ``to_dict()`` method (the matching
    scorer's :class:`ScoreBreakdown`) or a plain mapping. ``verdict`` is
    derived from ``breakdown.disqualified`` so callers don't have to
    pre-classify.
    """
    timestamp = now or datetime.now(UTC)

    breakdown_dict: dict[str, Any]
    if hasattr(breakdown, "to_dict"):
        breakdown_dict = breakdown.to_dict()
    elif isinstance(breakdown, dict):
        breakdown_dict = dict(breakdown)
    else:
        breakdown_dict = {"value": str(breakdown)}

    disqualified = bool(breakdown_dict.get("disqualified"))
    verdict = "reject" if disqualified else "surface"
    final_score_raw = breakdown_dict.get("final_score")
    final_score = float(final_score_raw) if isinstance(final_score_raw, (int, float)) else None

    insert_stmt = (
        pg_insert(JobPostingScore.__table__)
        .values(
            tenant_id=tenant_id,
            posting_id=posting_id,
            snapshot_id=snapshot_id,
            profile_id=profile_id,
            profile_version=profile_version,
            scorer_version=scorer_version,
            agent_version=agent_version or AGENT_VERSION,
            model_id=model_id,
            verdict=verdict,
            final_score=final_score,
            score_breakdown=breakdown_dict,
            computed_at=timestamp,
        )
        .on_conflict_do_update(
            constraint="uq_job_posting_scores_cache_key",
            set_={"computed_at": timestamp},
        )
        .returning(JobPostingScore.__table__.c.id)
    )
    result = session.execute(insert_stmt)
    row_id = result.scalar_one()
    session.flush()
    return session.get(JobPostingScore, row_id)


__all__ = [
    "AGENT_VERSION",
    "SCORER_VERSION",
    "CachedVerdict",
    "compute_profile_version",
    "get_cached_score",
    "record_score",
]
