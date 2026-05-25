"""Phase 19.6: filter fast-path.

The fast-path is read-only; we drive it through a stub session that
mirrors the call surface ``evaluate`` uses (``session.get`` for the
snapshot, ``get_cached_score`` patched to a dict lookup).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.filter import fast_path, score_cache


@dataclass
class _StubSnapshot:
    id: UUID
    posting_id: UUID
    tags: dict[str, Any] = field(default_factory=dict)
    tags_status: str = "ready"


class _StubSession:
    def __init__(self, snapshots: dict[UUID, _StubSnapshot]) -> None:
        self.snapshots = snapshots

    def get(self, _model, ident):
        return self.snapshots.get(ident)


@pytest.fixture
def patch_get_cached(monkeypatch):
    """Replace :func:`get_cached_score` with a dict-backed lookup so
    the fast-path test never touches SQLAlchemy."""
    cache: dict[tuple, Any] = {}

    def fake_get(
        session,
        *,
        tenant_id,
        snapshot_id,
        profile_id,
        profile_version,
        scorer_version=score_cache.SCORER_VERSION,
    ):
        return cache.get(
            (tenant_id, snapshot_id, profile_id, profile_version, scorer_version)
        )

    monkeypatch.setattr(fast_path, "get_cached_score", fake_get)
    return cache


def _make_snapshot(**kw) -> _StubSnapshot:
    return _StubSnapshot(id=uuid4(), posting_id=uuid4(), **kw)


# --- A1 rejection ---------------------------------------------------


def test_ready_tags_reject_when_sponsorship_required(patch_get_cached):
    snap = _make_snapshot(
        tags={"sponsorship_signal": "not_offered"}, tags_status="ready"
    )
    session = _StubSession({snap.id: snap})

    decision = fast_path.evaluate(
        session,
        snapshot_id=snap.id,
        tenant_id="default",
        profile_id="me",
        profile_version="v1",
        hard_rules=fast_path.HardRuleSet(rules=[fast_path.sponsorship_required_rule()]),
    )

    assert decision.kind == "reject_a1"
    assert decision.reason == "hard_rule_reject"
    assert decision.rule_id == "sponsorship_required"


def test_ready_tags_pass_when_sponsorship_offered(patch_get_cached):
    snap = _make_snapshot(
        tags={"sponsorship_signal": "offered"}, tags_status="ready"
    )
    session = _StubSession({snap.id: snap})

    decision = fast_path.evaluate(
        session,
        snapshot_id=snap.id,
        tenant_id="default",
        profile_id="me",
        profile_version="v1",
        hard_rules=fast_path.HardRuleSet(rules=[fast_path.sponsorship_required_rule()]),
    )

    assert decision.kind == "miss"
    assert decision.reason == "no_cache_row"


def test_clearance_rule_rejects_when_required(patch_get_cached):
    snap = _make_snapshot(tags={"clearance_required": True}, tags_status="ready")
    session = _StubSession({snap.id: snap})

    decision = fast_path.evaluate(
        session,
        snapshot_id=snap.id,
        tenant_id="default",
        profile_id="me",
        profile_version="v1",
        hard_rules=fast_path.HardRuleSet(rules=[fast_path.clearance_excluded_rule()]),
    )

    assert decision.is_rejected
    assert decision.rule_id == "no_clearance"


# --- pending / failed never reject ----------------------------------


@pytest.mark.parametrize("status,reason", [
    ("pending", "tags_not_ready"),
    ("computing", "tags_not_ready"),
    ("failed", "tags_failed"),
])
def test_non_ready_tags_never_reject(patch_get_cached, status, reason):
    """D029: pending/computing/failed must fall back to slow scoring
    rather than rejecting even if a tag superficially looks like it
    would fail a hard rule."""
    snap = _make_snapshot(
        tags={"sponsorship_signal": "not_offered"}, tags_status=status
    )
    session = _StubSession({snap.id: snap})

    decision = fast_path.evaluate(
        session,
        snapshot_id=snap.id,
        tenant_id="default",
        profile_id="me",
        profile_version="v1",
        hard_rules=fast_path.HardRuleSet(rules=[fast_path.sponsorship_required_rule()]),
    )

    assert decision.kind == "miss"
    assert decision.reason == reason


# --- A2 cache hit ---------------------------------------------------


def test_cache_hit_returns_cached_verdict(patch_get_cached):
    snap = _make_snapshot(tags={"work_mode": "remote"}, tags_status="ready")
    session = _StubSession({snap.id: snap})

    patch_get_cached[
        ("default", snap.id, "me", "v1", score_cache.SCORER_VERSION)
    ] = score_cache.CachedVerdict(
        snapshot_id=snap.id,
        profile_version="v1",
        scorer_version=score_cache.SCORER_VERSION,
        verdict="surface",
        final_score=0.82,
        score_breakdown={"final_score": 0.82, "skill_overlap": 0.9},
        computed_at=datetime.now(UTC),
    )

    decision = fast_path.evaluate(
        session,
        snapshot_id=snap.id,
        tenant_id="default",
        profile_id="me",
        profile_version="v1",
    )

    assert decision.is_cached
    assert decision.cached_verdict == "surface"
    assert decision.cached_final_score == 0.82
    assert decision.cached_profile_version == "v1"
    assert decision.cached_scorer_version == score_cache.SCORER_VERSION


def test_profile_version_mismatch_misses(patch_get_cached):
    snap = _make_snapshot(tags_status="ready")
    session = _StubSession({snap.id: snap})

    # Only v1 cached; we ask for v2.
    patch_get_cached[
        ("default", snap.id, "me", "v1", score_cache.SCORER_VERSION)
    ] = score_cache.CachedVerdict(
        snapshot_id=snap.id,
        profile_version="v1",
        scorer_version=score_cache.SCORER_VERSION,
        verdict="surface",
        final_score=0.5,
        score_breakdown={},
        computed_at=datetime.now(UTC),
    )

    decision = fast_path.evaluate(
        session,
        snapshot_id=snap.id,
        tenant_id="default",
        profile_id="me",
        profile_version="v2",
    )

    assert decision.is_miss


def test_no_snapshot(patch_get_cached):
    session = _StubSession({})
    decision = fast_path.evaluate(
        session,
        snapshot_id=uuid4(),
        tenant_id="default",
        profile_id="me",
        profile_version="v1",
    )
    assert decision.is_miss
    assert decision.reason == "no_snapshot"
