"""Phase 19 codex review fix: ``_default_score_fn`` must consult the A2
cache and skip the deterministic scorer for cache hits.

The first cut of Phase 19 wired the cache write-through but never read
the cache to short-circuit scoring. This test pins the new behaviour:
for raw_jobs whose ``(snapshot, profile_version, scorer_version)`` is
already cached, ``_default_score_fn`` rehydrates a ScoreBreakdown from
the cached row without invoking ``score_ranked``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from src.orchestration import plan_run


class _StubScoreBreakdown:
    """Mirrors the surface ``_breakdown_from_cached_verdict`` writes to."""

    def __init__(self, **kw):
        self.job_id = kw.get("job_id")
        self.company = kw.get("company", "")
        self.title = kw.get("title", "")
        self.job_snapshot_id = kw.get("job_snapshot_id")
        self.final_score = 0.0
        self.skill_overlap = 0.0
        self.keyword_similarity = 0.0
        self.rule_bonus = 0.0
        self.quality_multiplier = 1.0
        self.disqualified = False
        self.disqualify_reasons = []


def test_breakdown_from_cached_verdict_carries_cache_provenance():
    cached = SimpleNamespace(
        final_score=0.74,
        verdict="surface",
        score_breakdown={
            "final_score": 0.74,
            "disqualified": False,
            "skill_overlap": 0.9,
            "company": "Acme",
            "title": "SWE",
        },
        profile_version="abc123",
        scorer_version="s1",
        computed_at=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
    )
    raw_job = SimpleNamespace(id=uuid.uuid4(), company="Acme", title="SWE")
    posting_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    bd = plan_run._breakdown_from_cached_verdict(
        raw_job=raw_job,
        posting_id=posting_id,
        snapshot_id=snapshot_id,
        cached=cached,
        breakdown_cls=_StubScoreBreakdown,
    )

    assert bd.job_id == str(posting_id)
    assert bd.job_snapshot_id == str(snapshot_id)
    assert bd.final_score == 0.74
    assert bd.disqualified is False
    assert bd.skill_overlap == 0.9
    # Cache provenance is stashed for ReviewQueueView.
    payload = bd._cached_payload
    assert payload["cache"]["profile_version"] == "abc123"
    assert payload["cache"]["scorer_version"] == "s1"
    assert payload["cache"]["computed_at"].startswith("2026-05-25")


def test_breakdown_from_cached_reject_marks_disqualified():
    cached = SimpleNamespace(
        final_score=0.0,
        verdict="reject",
        score_breakdown={"final_score": 0.0, "disqualified": True},
        profile_version="v",
        scorer_version="s1",
        computed_at=datetime.now(UTC),
    )
    bd = plan_run._breakdown_from_cached_verdict(
        raw_job=SimpleNamespace(id=uuid.uuid4(), company="X", title="Y"),
        posting_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        cached=cached,
        breakdown_cls=_StubScoreBreakdown,
    )
    assert bd.disqualified is True
    assert bd.final_score == 0.0


def test_consume_score_cache_skips_scorer_on_hit(monkeypatch):
    """When the cache pre-pass finds a hit, the scorer should not run
    for that job. We stub session + cache helpers; the test then asserts
    that ``_consume_score_cache`` returns one hit and zero misses."""
    raw_job = SimpleNamespace(
        id=uuid.uuid4(), source="linkedin", source_id="abc", company="X", title="Y"
    )
    posting_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    # Fake session + execute().all() returning the one resolution row.
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, _stmt):
            self.execute_calls += 1
            return _Result(
                [
                    SimpleNamespace(
                        id=posting_id,
                        latest_snapshot_id=snapshot_id,
                        source="linkedin",
                        source_id="abc",
                    )
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        "src.core.database.get_session_factory",
        lambda *a, **kw: lambda: _Session(),
    )

    cached_row = SimpleNamespace(
        final_score=0.91,
        verdict="surface",
        score_breakdown={"final_score": 0.91},
        profile_version=plan_run._coerce_uuid  # placeholder; overwritten below
        and "v1",
        scorer_version="s1",
        computed_at=datetime.now(UTC),
    )
    cached_row.profile_version = "v1"

    monkeypatch.setattr(
        "src.filter.score_cache.get_cached_score",
        lambda *a, **kw: cached_row,
    )
    monkeypatch.setattr(
        "src.filter.score_cache.compute_profile_version",
        lambda _profile: "v1",
    )

    hits, misses, resolution = plan_run._consume_score_cache(
        raw_jobs=[raw_job],
        tenant_id="default",
        profile_id="me",
        profile_data={"k": "v"},
    )

    assert len(hits) == 1
    assert hits[0].final_score == 0.91
    assert misses == []
    assert resolution[str(raw_job.id)] == (posting_id, snapshot_id)


def test_consume_score_cache_misses_when_no_snapshot(monkeypatch):
    raw_job = SimpleNamespace(
        id=uuid.uuid4(), source="linkedin", source_id="abc", company="X", title="Y"
    )

    class _Result:
        def all(self):
            return []

    class _Session:
        def execute(self, _stmt):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        "src.core.database.get_session_factory",
        lambda *a, **kw: lambda: _Session(),
    )
    monkeypatch.setattr(
        "src.filter.score_cache.compute_profile_version",
        lambda _profile: "v1",
    )

    def _should_not_be_called(*a, **kw):
        raise AssertionError("get_cached_score must not be called when no snapshot resolves")

    monkeypatch.setattr(
        "src.filter.score_cache.get_cached_score", _should_not_be_called
    )

    hits, misses, resolution = plan_run._consume_score_cache(
        raw_jobs=[raw_job],
        tenant_id="default",
        profile_id="me",
        profile_data={},
    )
    assert hits == []
    assert len(misses) == 1
    assert resolution == {}
