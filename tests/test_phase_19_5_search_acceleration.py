"""Phase 19.5 search acceleration contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from src.filter.score_cache import get_cached_score
from src.jobs.cache_policy import search_acceleration_policy
from src.orchestration import plan_run


def test_policy_always_refreshes_upstream_and_normalizes_ttl():
    enabled = search_acceleration_policy({"search_cache": {"enabled": True, "ttl_hours": "12"}})
    disabled = search_acceleration_policy({"search_cache": {"enabled": False, "ttl_hours": 0}})

    assert enabled == {
        "enabled": True,
        "ttl_hours": 12,
        "mode": "acceleration_only",
        "upstream_refresh": "always",
    }
    assert disabled["enabled"] is False
    assert disabled["ttl_hours"] == 1
    assert disabled["upstream_refresh"] == "always"


def _install_score_dependencies(monkeypatch, *, policy: dict, scored: list):
    monkeypatch.setattr("src.application.profile.get_profile_path", lambda _id: Path("README.md"))
    monkeypatch.setattr("src.memory.profile.load_profile_yaml", lambda _path: {"name": "A"})
    monkeypatch.setattr("src.matching.scorer.build_scoring_context", lambda _profile: object())
    monkeypatch.setattr("src.matching.scorer.score_jobs", lambda jobs, _ctx: scored if jobs else [])
    monkeypatch.setattr("src.jobs.cache_policy.search_acceleration_policy", lambda: policy)
    monkeypatch.setattr(plan_run, "_resolve_and_patch_posting_ids", lambda *a, **kw: None)
    monkeypatch.setattr(plan_run, "_apply_a1_rejects", lambda *a, **kw: None)
    monkeypatch.setattr(plan_run, "_persist_score_cache", lambda *a, **kw: None)


def test_disabled_acceleration_bypasses_score_cache(monkeypatch):
    breakdown = SimpleNamespace(final_score=0.7)
    _install_score_dependencies(
        monkeypatch,
        policy={"enabled": False, "ttl_hours": 24},
        scored=[breakdown],
    )
    monkeypatch.setattr(
        plan_run,
        "_consume_score_cache",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("cache read must be bypassed")),
    )

    result = plan_run._default_score_fn([SimpleNamespace(id=uuid4())], "me", tenant_id="t")

    assert result == [breakdown]


def test_enabled_acceleration_forwards_reuse_ttl(monkeypatch):
    raw_job = SimpleNamespace(id=uuid4())
    breakdown = SimpleNamespace(final_score=0.7)
    _install_score_dependencies(
        monkeypatch,
        policy={"enabled": True, "ttl_hours": 6},
        scored=[breakdown],
    )
    observed = {}

    def consume(**kwargs):
        observed.update(kwargs)
        return [], [raw_job], {}

    monkeypatch.setattr(plan_run, "_consume_score_cache", consume)
    monkeypatch.setattr(plan_run, "_consume_a1_rejects", lambda **kw: ([], kw["raw_jobs"]))

    result = plan_run._default_score_fn([raw_job], "me", tenant_id="t")

    assert result == [breakdown]
    assert observed["max_age_hours"] == 6


def test_score_cache_max_age_adds_computed_at_cutoff():
    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return Result()

    session = Session()
    result = get_cached_score(
        session,
        tenant_id="t",
        snapshot_id=uuid4(),
        profile_id="me",
        profile_version="v1",
        max_age_hours=6,
        now=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )

    assert result is None
    assert "computed_at" in str(session.statement)
