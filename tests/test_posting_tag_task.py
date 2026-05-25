"""Phase 19.3: smoke tests for the ``posting.tag`` and
``posting.tag_backfill`` Celery tasks + the on_content_changed listener.

We patch the DB session helper to return a stub so the eager-mode task
body exercises the wiring without Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.jobs import enrich as enrich_mod
from src.jobs import listeners as listeners_mod
from src.tasks import tasks as task_kinds
from src.tasks.app import celery_app


@pytest.fixture(autouse=True)
def _eager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)


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
    title: str = "Senior Backend Engineer"
    location: str | None = "Remote"
    employment_type: str | None = None
    seniority: str | None = None
    description: str | None = "We do not sponsor visas."
    requirements: dict | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tags: dict[str, Any] = field(default_factory=dict)
    tagger_version: int = 0
    tags_status: str = "pending"
    tags_computed_at: datetime | None = None


class _StubSession:
    def __init__(self, snapshots, postings) -> None:
        self.snapshots = snapshots
        self.postings = postings
        self.flushes = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def begin(self):
        yield

    def get(self, model, ident):
        if ident in self.snapshots:
            return self.snapshots[ident]
        if ident in self.postings:
            return self.postings[ident]
        return None

    def flush(self) -> None:
        self.flushes += 1

    # ``select_stale_snapshots`` calls session.execute(...). The
    # backfill test patches the helper directly to avoid faking
    # SQLAlchemy's execute surface.


def _install_factory(monkeypatch, snapshots, postings):
    def factory():
        return _StubSession(snapshots, postings)

    def get_factory(*_a, **_kw):
        return factory

    monkeypatch.setattr("src.core.database.get_session_factory", get_factory)


def test_posting_tag_registered_and_routed():
    assert "posting.tag" in celery_app.tasks
    assert "posting.tag_backfill" in celery_app.tasks
    from src.tasks.app import _task_router

    assert _task_router("posting.tag") == {"queue": "search"}
    assert _task_router("posting.tag_backfill") == {"queue": "search"}


def test_posting_tag_writes_ready(monkeypatch):
    snap_id = uuid4()
    posting_id = uuid4()
    snapshot = _StubSnapshot(id=snap_id, posting_id=posting_id)
    posting = _StubPosting(id=posting_id, latest_snapshot_id=snap_id)
    _install_factory(monkeypatch, {snap_id: snapshot}, {posting_id: posting})

    out = task_kinds.posting_tag.apply(kwargs={"snapshot_id": str(snap_id)}).get()

    assert out["task"] == "posting.tag"
    assert out["status"] == "ok"
    assert out["tags_status"] == "ready"
    assert out["tags"]["work_mode"] == "remote"
    assert out["tags"]["sponsorship_signal"] == "not_offered"
    assert posting.latest_tags["work_mode"] == "remote"


def test_posting_tag_invalid_id_returns_terminal_envelope():
    out = task_kinds.posting_tag.apply(kwargs={"snapshot_id": "not-a-uuid"}).get()
    assert out["status"] == "invalid_snapshot_id"


def test_posting_tag_missing_snapshot(monkeypatch):
    _install_factory(monkeypatch, {}, {})
    out = task_kinds.posting_tag.apply(kwargs={"snapshot_id": str(uuid4())}).get()
    assert out["status"] == "snapshot_not_found"


def test_posting_tag_backfill_consumes_a_batch(monkeypatch):
    snap_id = uuid4()
    posting_id = uuid4()
    snapshot = _StubSnapshot(id=snap_id, posting_id=posting_id)
    posting = _StubPosting(id=posting_id, latest_snapshot_id=snap_id)
    _install_factory(monkeypatch, {snap_id: snapshot}, {posting_id: posting})

    from src.jobs import tag_service

    monkeypatch.setattr(
        tag_service,
        "select_stale_snapshots",
        lambda session, *, tenant_id, batch_size: tag_service.BackfillSlice(
            snapshot_ids=[snap_id], remaining_estimate=1
        ),
    )

    out = task_kinds.posting_tag_backfill.apply(
        kwargs={"batch_size": 10, "max_batches": 1}
    ).get()
    assert out["task"] == "posting.tag_backfill"
    assert out["processed"] == 1
    assert out["failed"] == 0
    assert out["remaining_estimate"] == 0
    assert snapshot.tags_status == "ready"


def test_content_changed_listener_enqueues_posting_tag(monkeypatch):
    """When :func:`enrich_posting` fires a ContentChangedEvent the
    production listener should enqueue ``posting.tag``."""
    listeners_mod.reinstall_for_tests()
    enrich_mod.reset_listeners()
    listeners_mod.install()

    sent: list[dict[str, Any]] = []

    def fake_send_task(name, *, kwargs, queue=None):
        sent.append({"name": name, "kwargs": kwargs, "queue": queue})

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)

    event = enrich_mod.ContentChangedEvent(
        posting_id=uuid4(),
        snapshot_id=uuid4(),
        previous_snapshot_id=None,
        content_hash="abc",
        scraped_at=datetime.now(UTC),
    )
    enrich_mod._emit(event)

    assert sent, "listener should have enqueued posting.tag"
    assert sent[0]["name"] == "posting.tag"
    assert sent[0]["kwargs"] == {"snapshot_id": str(event.snapshot_id)}
    assert sent[0]["queue"] == "search"

    # cleanup
    enrich_mod.reset_listeners()
    listeners_mod.reinstall_for_tests()
    listeners_mod.install()
