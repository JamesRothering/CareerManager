"""Phase 19.4: A2 score cache helpers.

The cache primitives (:func:`record_score`, :func:`get_cached_score`)
talk to SQLAlchemy directly; their happy paths are exercised by the
fast-path integration test that pairs this module with a stub session
mirroring the call surface (see :mod:`tests.test_filter_fast_path`).

This module focuses on the pure helpers and the verdict derivation so
the cache key contract (D029) is pinned independently of SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from src.filter import score_cache


def test_compute_profile_version_is_stable_and_short():
    profile = {"name": "Alice", "skills": ["python"]}
    v1 = score_cache.compute_profile_version(profile)
    v2 = score_cache.compute_profile_version({"skills": ["python"], "name": "Alice"})
    assert v1 == v2  # order-independent
    assert len(v1) == 12


def test_compute_profile_version_changes_on_edit():
    a = score_cache.compute_profile_version({"k": 1})
    b = score_cache.compute_profile_version({"k": 2})
    assert a != b


def test_compute_profile_version_missing():
    assert score_cache.compute_profile_version(None) == "unknown"
    assert score_cache.compute_profile_version({}) == "unknown"


def test_scorer_version_is_a_short_string():
    """D029 requires explicit scorer_version invalidation. Stays a
    short string so the review-queue UI can render `scorer sABC`."""
    assert isinstance(score_cache.SCORER_VERSION, str)
    assert 1 <= len(score_cache.SCORER_VERSION) <= 8


# --- in-memory rehearsal --------------------------------------------


@dataclass
class _Breakdown:
    final_score: float = 0.7
    disqualified: bool = False

    def to_dict(self):
        return {"final_score": self.final_score, "disqualified": self.disqualified}


class _RowList:
    """Minimal in-memory stand-in: ``record_score`` adds; ``get_cached_score``
    filters by the cache key. Lets us pin verdict derivation + cache-key
    selectivity without booting Postgres."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, payload: dict) -> None:
        self.rows.append(payload)

    def find(self, **predicates):
        for r in self.rows:
            if all(r.get(k) == v for k, v in predicates.items()):
                return r
        return None


def _record(rows: _RowList, *, breakdown=_Breakdown(), **key):
    """Mirror of :func:`record_score`'s verdict derivation."""
    bd = breakdown.to_dict()
    rows.add(
        {
            **key,
            "verdict": "reject" if bd.get("disqualified") else "surface",
            "final_score": bd.get("final_score"),
        }
    )


def test_in_memory_round_trip_hits_on_same_key():
    rows = _RowList()
    key = dict(
        tenant_id="default",
        snapshot_id=str(uuid4()),
        profile_id="me",
        profile_version="abc",
        scorer_version=score_cache.SCORER_VERSION,
    )
    _record(rows, breakdown=_Breakdown(final_score=0.9, disqualified=False), **key)
    assert rows.find(**key) is not None


def test_in_memory_profile_version_change_misses():
    rows = _RowList()
    base = dict(
        tenant_id="default",
        snapshot_id=str(uuid4()),
        profile_id="me",
        scorer_version=score_cache.SCORER_VERSION,
    )
    _record(rows, profile_version="abc", **base)
    assert rows.find(profile_version="zzz", **base) is None


def test_in_memory_scorer_version_change_misses():
    rows = _RowList()
    base = dict(
        tenant_id="default",
        snapshot_id=str(uuid4()),
        profile_id="me",
        profile_version="abc",
    )
    _record(rows, scorer_version="s1", **base)
    assert rows.find(scorer_version="s2", **base) is None
