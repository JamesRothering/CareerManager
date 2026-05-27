"""Phase 19.6: filter fast-path.

Reads two caches before invoking the deterministic / agent scoring path:

1. **A1 snapshot tags** (``JobSnapshot.tags``) for hard-rule rejects.
   ``tags_status='ready'`` is required for a reject; ``pending`` /
   ``computing`` show the posting with a "Tagging…" hint and fall back
   to slow scoring; ``failed`` is treated the same way (never default
   reject from a failed tag).

2. **A2 score cache** (``job_posting_scores``) keyed by
   ``(tenant_id, snapshot_id, profile_id, profile_version, scorer_version)``.
   A hit returns a cached :class:`FastPathDecision` carrying enough
   metadata for the review-queue UI to render
   "(cached score · profile vXYZ · scorer sABC)".

A miss returns ``kind="miss"`` plus a structured reason; the caller is
expected to run the real scorer (and feed the result through
:func:`src.filter.score_cache.record_score`).

Boundaries (D029):

* The fast-path never default-rejects on missing / failed tags. The
  spec is explicit: tags ``pending``/``computing``/``failed`` fall back
  to slow scoring rather than blocking the user from seeing the
  posting.
* Profile-version / scorer-version mismatches always miss. Old rows
  remain queryable for audit, but the hot path uses only current
  versions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from src.core.models import JobSnapshot
from src.filter.score_cache import (
    SCORER_VERSION,
    CachedVerdict,
    get_cached_score,
)
from src.jobs.tag_service import (
    STATUS_COMPUTING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
)

logger = logging.getLogger("autoapply.filter.fast_path")


FastPathKind = Literal[
    "reject_a1",   # snapshot tag failed a hard rule -- skip scoring
    "cached",      # A2 cache hit -- reuse the verdict
    "miss",        # caller must run the real scorer
]

FastPathReason = Literal[
    "hard_rule_reject",
    "cache_hit",
    "tags_not_ready",
    "tags_failed",
    "no_snapshot",
    "no_cache_row",
    "profile_version_mismatch",
    "scorer_version_mismatch",
]


@dataclass
class HardRule:
    """One A1 hard-rule check. Returning ``True`` from :meth:`apply`
    means the snapshot fails the rule and should be rejected up-front.
    """

    rule_id: str
    description: str

    def apply(self, tags: dict[str, Any]) -> bool:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass
class HardRuleSet:
    """Composable set of hard rules the caller passes to :func:`evaluate`.

    Empty by default: callers opt in to the rules they want enforced so
    the fast-path stays a pure boundary -- this module never decides
    *which* tags should reject.
    """

    rules: list[HardRule] = field(default_factory=list)

    def first_failure(self, tags: dict[str, Any]) -> HardRule | None:
        for rule in self.rules:
            try:
                if rule.apply(tags):
                    return rule
            except Exception as exc:  # noqa: BLE001
                logger.warning("hard rule %s raised: %s", rule.rule_id, exc)
        return None


@dataclass
class _SponsorshipRequired(HardRule):
    """Reject when the JD explicitly says no sponsorship.

    Default ``rule_id`` / ``description`` make this the typical "I need
    sponsorship" hard rule plan runs install when the applicant
    requires visa support.
    """

    rule_id: str = "sponsorship_required"
    description: str = "Applicant requires visa sponsorship."

    def apply(self, tags: dict[str, Any]) -> bool:
        return tags.get("sponsorship_signal") == "not_offered"


@dataclass
class _ExcludeClearance(HardRule):
    rule_id: str = "no_clearance"
    description: str = "Applicant cannot obtain a US security clearance."

    def apply(self, tags: dict[str, Any]) -> bool:
        return bool(tags.get("clearance_required"))


def sponsorship_required_rule() -> HardRule:
    return _SponsorshipRequired()


def clearance_excluded_rule() -> HardRule:
    return _ExcludeClearance()


@dataclass
class FastPathDecision:
    """Outcome of one :func:`evaluate` call.

    The caller decides what to do with each ``kind``:

    * ``reject_a1`` --- skip the real scorer, render a "filtered by
      <rule_id>" badge in the review queue;
    * ``cached`` --- reuse the verdict (no LLM / no rescoring);
    * ``miss`` --- run the real scorer.
    """

    kind: FastPathKind
    reason: FastPathReason
    snapshot_id: UUID | None = None
    posting_id: UUID | None = None
    rule_id: str | None = None
    rule_description: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    tags_status: str | None = None
    cached_verdict: str | None = None
    cached_final_score: float | None = None
    cached_breakdown: dict[str, Any] = field(default_factory=dict)
    cached_profile_version: str | None = None
    cached_scorer_version: str | None = None
    cached_computed_at: datetime | None = None

    @property
    def is_cached(self) -> bool:
        return self.kind == "cached"

    @property
    def is_rejected(self) -> bool:
        return self.kind == "reject_a1"

    @property
    def is_miss(self) -> bool:
        return self.kind == "miss"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "posting_id": str(self.posting_id) if self.posting_id else None,
            "rule_id": self.rule_id,
            "rule_description": self.rule_description,
            "tags": dict(self.tags),
            "tags_status": self.tags_status,
            "cached_verdict": self.cached_verdict,
            "cached_final_score": self.cached_final_score,
            "cached_breakdown": dict(self.cached_breakdown),
            "cached_profile_version": self.cached_profile_version,
            "cached_scorer_version": self.cached_scorer_version,
            "cached_computed_at": (
                self.cached_computed_at.isoformat() if self.cached_computed_at else None
            ),
        }


def evaluate(
    session: Session,
    *,
    snapshot_id: UUID,
    tenant_id: str,
    profile_id: str,
    profile_version: str,
    hard_rules: HardRuleSet | None = None,
    scorer_version: str = SCORER_VERSION,
) -> FastPathDecision:
    """Look up A1 tags + A2 score for one snapshot. See module docstring.

    Returns a :class:`FastPathDecision` describing exactly what the
    caller should do next. The function is read-only --- writes belong
    to :mod:`src.filter.score_cache` after the slow path runs.
    """
    snapshot = session.get(JobSnapshot, snapshot_id)
    if snapshot is None:
        return FastPathDecision(
            kind="miss",
            reason="no_snapshot",
            snapshot_id=snapshot_id,
        )

    tags = dict(snapshot.tags or {})
    tags_status = snapshot.tags_status

    # ---- A1: hard-rule rejects only when tags are READY ----
    if tags_status == STATUS_READY and hard_rules is not None:
        failed = hard_rules.first_failure(tags)
        if failed is not None:
            return FastPathDecision(
                kind="reject_a1",
                reason="hard_rule_reject",
                snapshot_id=snapshot.id,
                posting_id=snapshot.posting_id,
                rule_id=failed.rule_id,
                rule_description=failed.description,
                tags=tags,
                tags_status=tags_status,
            )

    if tags_status in (STATUS_PENDING, STATUS_COMPUTING):
        # Fall through to A2 cache lookup but never reject on tags --
        # the UI shows "Tagging…" and the slow path takes over.
        a2_reason: FastPathReason = "tags_not_ready"
    elif tags_status == STATUS_FAILED:
        a2_reason = "tags_failed"
    else:
        a2_reason = "no_cache_row"

    # ---- A2: score cache lookup ----
    cached: CachedVerdict | None = get_cached_score(
        session,
        tenant_id=tenant_id,
        snapshot_id=snapshot.id,
        profile_id=profile_id,
        profile_version=profile_version,
        scorer_version=scorer_version,
    )
    if cached is not None:
        return FastPathDecision(
            kind="cached",
            reason="cache_hit",
            snapshot_id=snapshot.id,
            posting_id=snapshot.posting_id,
            tags=tags,
            tags_status=tags_status,
            cached_verdict=cached.verdict,
            cached_final_score=cached.final_score,
            cached_breakdown=cached.score_breakdown,
            cached_profile_version=cached.profile_version,
            cached_scorer_version=cached.scorer_version,
            cached_computed_at=cached.computed_at,
        )

    return FastPathDecision(
        kind="miss",
        reason=a2_reason,
        snapshot_id=snapshot.id,
        posting_id=snapshot.posting_id,
        tags=tags,
        tags_status=tags_status,
    )


__all__ = [
    "FastPathDecision",
    "FastPathKind",
    "FastPathReason",
    "HardRule",
    "HardRuleSet",
    "clearance_excluded_rule",
    "evaluate",
    "sponsorship_required_rule",
]
