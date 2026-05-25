"""Phase 19 codex review fix: ``record_score`` must stay batch-safe.

The Phase 19 first cut caught ``IntegrityError`` and called
``session.rollback()`` to handle a duplicate cache key. Inside the
``with session.begin()`` block opened by
``plan_run._persist_score_cache``, that rollback closes the outer
transaction --- the second insert in the batch then explodes with
"this transaction is closed". The fix is to use Postgres
``ON CONFLICT DO NOTHING`` so the conflict never triggers a rollback;
this test pins that behaviour against a real Postgres session.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import load_config
from src.core.database import get_db_url
from src.core.models import JobPosting, JobPostingScore, JobSnapshot
from src.filter import score_cache

_TENANT = "codex-19-test"


@pytest.fixture(scope="module")
def engine():
    return create_engine(get_db_url(load_config()))


@pytest.fixture
def db_session(engine) -> Session:
    factory = sessionmaker(bind=engine)
    s = factory()
    yield s
    s.execute(delete(JobPostingScore).where(JobPostingScore.tenant_id == _TENANT))
    s.execute(
        delete(JobSnapshot).where(
            JobSnapshot.posting_id.in_(
                s.query(JobPosting.id).filter(JobPosting.tenant_id == _TENANT)
            )
        )
    )
    s.execute(delete(JobPosting).where(JobPosting.tenant_id == _TENANT))
    s.commit()
    s.close()


def _make_posting_with_snapshot(s: Session, *, source_id: str) -> JobSnapshot:
    posting = JobPosting(
        tenant_id=_TENANT,
        source="test",
        source_id=source_id,
        company="Acme",
        state="active",
    )
    s.add(posting)
    s.flush()
    snap = JobSnapshot(
        tenant_id=_TENANT,
        posting_id=posting.id,
        content_hash=f"hash-{source_id}",
        title="Engineer",
    )
    s.add(snap)
    s.flush()
    return snap


class _StubBreakdown:
    def __init__(self, score: float, disqualified: bool = False) -> None:
        self.score = score
        self.disqualified = disqualified

    def to_dict(self) -> dict:
        return {"final_score": self.score, "disqualified": self.disqualified}


def test_record_score_handles_duplicate_then_continues_batch(db_session: Session) -> None:
    """The transaction must stay open after a duplicate so subsequent
    inserts in the same batch still land. This is the codex-found
    regression: ``session.rollback()`` on IntegrityError closed the
    outer ``session.begin()``."""
    snap_a = _make_posting_with_snapshot(db_session, source_id="A")
    snap_b = _make_posting_with_snapshot(db_session, source_id="B")
    db_session.commit()

    # Seed an existing row for snap_a -- this becomes the duplicate.
    with db_session.begin():
        score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap_a.posting_id,
            snapshot_id=snap_a.id,
            profile_id="me",
            profile_version="v1",
            breakdown=_StubBreakdown(0.7),
        )

    # New transaction simulating _persist_score_cache: re-insert
    # (duplicate) then add a brand new row for snap_b. The second
    # call must succeed.
    with db_session.begin():
        score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap_a.posting_id,
            snapshot_id=snap_a.id,
            profile_id="me",
            profile_version="v1",
            breakdown=_StubBreakdown(0.99),  # different value; on-conflict keeps original
        )
        score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap_b.posting_id,
            snapshot_id=snap_b.id,
            profile_id="me",
            profile_version="v1",
            breakdown=_StubBreakdown(0.5),
        )

    # Both rows present; snap_a still has the original 0.7 because
    # on-conflict is DO NOTHING (we don't want to silently overwrite
    # an audit-relevant earlier verdict).
    hit_a = score_cache.get_cached_score(
        db_session,
        tenant_id=_TENANT,
        snapshot_id=snap_a.id,
        profile_id="me",
        profile_version="v1",
    )
    hit_b = score_cache.get_cached_score(
        db_session,
        tenant_id=_TENANT,
        snapshot_id=snap_b.id,
        profile_id="me",
        profile_version="v1",
    )
    assert hit_a is not None
    assert hit_a.final_score == 0.7
    assert hit_b is not None
    assert hit_b.final_score == 0.5


def test_record_score_returns_existing_row_on_conflict(db_session: Session) -> None:
    snap = _make_posting_with_snapshot(db_session, source_id="C")
    db_session.commit()

    with db_session.begin():
        first = score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap.posting_id,
            snapshot_id=snap.id,
            profile_id="me",
            profile_version="v1",
            breakdown=_StubBreakdown(0.3),
        )

    with db_session.begin():
        second = score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap.posting_id,
            snapshot_id=snap.id,
            profile_id="me",
            profile_version="v1",
            breakdown=_StubBreakdown(0.8),
        )

    assert first.id == second.id
    assert second.final_score == 0.3  # original wins under DO NOTHING


def test_record_score_different_profile_version_inserts_new_row(db_session: Session) -> None:
    snap = _make_posting_with_snapshot(db_session, source_id="D")
    db_session.commit()

    with db_session.begin():
        score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap.posting_id,
            snapshot_id=snap.id,
            profile_id="me",
            profile_version="v1",
            breakdown=_StubBreakdown(0.3),
        )
        score_cache.record_score(
            db_session,
            tenant_id=_TENANT,
            posting_id=snap.posting_id,
            snapshot_id=snap.id,
            profile_id="me",
            profile_version="v2",
            breakdown=_StubBreakdown(0.6),
        )

    v1 = score_cache.get_cached_score(
        db_session, tenant_id=_TENANT, snapshot_id=snap.id, profile_id="me", profile_version="v1"
    )
    v2 = score_cache.get_cached_score(
        db_session, tenant_id=_TENANT, snapshot_id=snap.id, profile_id="me", profile_version="v2"
    )
    assert v1 is not None and v1.final_score == 0.3
    assert v2 is not None and v2.final_score == 0.6
