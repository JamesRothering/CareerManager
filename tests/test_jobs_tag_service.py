"""Phase 19.3: tag service + posting.tag/posting.tag_backfill tasks.

We stub out the DB session so the tests pin the wiring (status writes,
mirror to latest_tags, backfill pagination) without needing Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.jobs import tag_service
from src.jobs.tagger import TAGGER_VERSION


@dataclass
class _StubPosting:
    id: UUID
    latest_snapshot_id: UUID | None = None
    latest_tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class _StubSnapshot:
    id: UUID
    posting_id: UUID
    tenant_id: str = "default"
    title: str = "Software Engineer"
    location: str | None = "Remote"
    employment_type: str | None = None
    seniority: str | None = None
    description: str | None = "We are hiring."
    requirements: dict | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tags: dict[str, Any] = field(default_factory=dict)
    tagger_version: int = 0
    tags_status: str = "pending"
    tags_computed_at: datetime | None = None


class _StubSession:
    def __init__(self) -> None:
        self.snapshots: dict[UUID, _StubSnapshot] = {}
        self.postings: dict[UUID, _StubPosting] = {}
        self.flushes = 0

    def get(self, model, ident):
        # We don't care about the SQLAlchemy class identity here -- the
        # service code only calls .get with `JobSnapshot` or
        # `JobPosting`. We disambiguate by the dict that owns the id.
        if ident in self.snapshots:
            return self.snapshots[ident]
        if ident in self.postings:
            return self.postings[ident]
        return None

    def flush(self) -> None:
        self.flushes += 1


def _make(session: _StubSession, *, latest: bool = True) -> tuple[_StubPosting, _StubSnapshot]:
    posting_id = uuid4()
    snapshot_id = uuid4()
    posting = _StubPosting(
        id=posting_id, latest_snapshot_id=snapshot_id if latest else None
    )
    snapshot = _StubSnapshot(id=snapshot_id, posting_id=posting_id)
    session.postings[posting_id] = posting
    session.snapshots[snapshot_id] = snapshot
    return posting, snapshot


def test_tag_snapshot_marks_ready_and_mirrors_to_posting():
    session = _StubSession()
    posting, snapshot = _make(session)

    result = tag_service.tag_snapshot(session, snapshot.id)

    assert result.status == "ready"
    assert result.tagger_version == TAGGER_VERSION
    assert snapshot.tags_status == "ready"
    assert snapshot.tagger_version == TAGGER_VERSION
    assert snapshot.tags == result.tags
    # latest -> mirrored to posting.latest_tags
    assert posting.latest_tags == result.tags
    assert session.flushes >= 1


def test_tag_snapshot_does_not_mirror_when_snapshot_is_not_latest():
    session = _StubSession()
    posting, snapshot = _make(session, latest=False)

    result = tag_service.tag_snapshot(session, snapshot.id)

    assert result.status == "ready"
    # The posting still has an empty latest_tags because this snapshot
    # is not the latest_snapshot_id.
    assert posting.latest_tags == {}


def test_tag_snapshot_records_failure_without_raising(monkeypatch):
    session = _StubSession()
    _, snapshot = _make(session)

    def boom(*_a, **_k):
        raise RuntimeError("tagger blew up")

    monkeypatch.setattr(tag_service, "compute_tags", boom)
    result = tag_service.tag_snapshot(session, snapshot.id)

    assert result.status == "failed"
    assert "tagger blew up" in (result.error or "")
    assert snapshot.tags_status == "failed"


def test_tag_snapshot_missing_raises_lookup_error():
    session = _StubSession()
    with pytest.raises(LookupError):
        tag_service.tag_snapshot(session, uuid4())
