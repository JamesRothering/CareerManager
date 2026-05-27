"""Phase 19.3: thin service layer between the tagger and the database.

The Celery task body and the on-content-changed listener both share this
module so the "compute + write tags" step has one home. Keeping it out of
:mod:`src.jobs.tagger` lets the tagger stay a pure-function unit
(profile-independent, no I/O) and lets the task body stay a thin Celery
wrapper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.core.models import JobPosting, JobSnapshot
from src.jobs.tagger import (
    TAGGER_VERSION,
    compute_tags,
    snapshot_view_from_row,
)

logger = logging.getLogger("autoapply.jobs.tag_service")


# ``tags_status`` vocabulary. Kept here as constants so callers don't
# scatter string literals through the codebase.
STATUS_PENDING = "pending"
STATUS_COMPUTING = "computing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


@dataclass
class TagWriteResult:
    snapshot_id: UUID
    posting_id: UUID
    status: str
    tagger_version: int
    tags: dict[str, Any]
    error: str | None = None


def tag_snapshot(
    session: Session,
    snapshot_id: UUID,
    *,
    now: datetime | None = None,
) -> TagWriteResult:
    """Compute + persist tags for a single snapshot row.

    Writes the snapshot's ``tags`` / ``tagger_version`` / ``tags_status`` /
    ``tags_computed_at`` and (when the snapshot is still the posting's
    latest) mirrors ``tags`` to ``JobPosting.latest_tags`` so the JobsView
    listing avoids the snapshot join. Caller owns the transaction
    boundary; we ``flush()`` so ``select`` calls in the same unit-of-work
    see the new values.
    """
    snapshot = session.get(JobSnapshot, snapshot_id)
    if snapshot is None:
        raise LookupError(f"snapshot {snapshot_id} not found")

    timestamp = now or datetime.now(UTC)
    view = snapshot_view_from_row(snapshot)

    try:
        tags = compute_tags(view, now=timestamp)
    except Exception as exc:  # noqa: BLE001 -- record failure, never re-raise
        logger.exception("tag_service: tagger raised for snapshot %s", snapshot_id)
        snapshot.tags_status = STATUS_FAILED
        snapshot.tags_computed_at = timestamp
        session.flush()
        return TagWriteResult(
            snapshot_id=snapshot.id,
            posting_id=snapshot.posting_id,
            status=STATUS_FAILED,
            tagger_version=snapshot.tagger_version,
            tags=dict(snapshot.tags or {}),
            error=f"{type(exc).__name__}: {exc}",
        )

    snapshot.tags = tags
    snapshot.tagger_version = TAGGER_VERSION
    snapshot.tags_status = STATUS_READY
    snapshot.tags_computed_at = timestamp

    posting = session.get(JobPosting, snapshot.posting_id)
    if posting is not None and posting.latest_snapshot_id == snapshot.id:
        posting.latest_tags = tags

    session.flush()
    return TagWriteResult(
        snapshot_id=snapshot.id,
        posting_id=snapshot.posting_id,
        status=STATUS_READY,
        tagger_version=TAGGER_VERSION,
        tags=tags,
    )


def mark_pending(session: Session, snapshot_id: UUID) -> None:
    """Stamp a snapshot back to ``pending`` so the fast-path falls
    through to slow scoring while the next ``posting.tag`` runs."""
    session.execute(
        update(JobSnapshot)
        .where(JobSnapshot.id == snapshot_id)
        .values(tags_status=STATUS_PENDING)
    )


@dataclass
class BackfillSlice:
    snapshot_ids: list[UUID]
    remaining_estimate: int


def select_stale_snapshots(
    session: Session,
    *,
    tenant_id: str,
    batch_size: int = 100,
) -> BackfillSlice:
    """Return the next batch of snapshots whose tagger_version trails
    :data:`TAGGER_VERSION`.

    Ordered by ``id`` so successive backfill ticks make monotonic
    progress instead of fighting over the same hot rows. The "remaining"
    estimate is a cheap unbounded count for the UI banner.
    """
    stmt = (
        select(JobSnapshot.id)
        .where(JobSnapshot.tenant_id == tenant_id)
        .where(JobSnapshot.tagger_version < TAGGER_VERSION)
        .order_by(JobSnapshot.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    snapshot_ids = list(session.execute(stmt).scalars())

    from sqlalchemy import func  # noqa: PLC0415 -- local import keeps module light

    count_stmt = (
        select(func.count())
        .select_from(JobSnapshot)
        .where(JobSnapshot.tenant_id == tenant_id)
        .where(JobSnapshot.tagger_version < TAGGER_VERSION)
    )
    remaining = int(session.execute(count_stmt).scalar() or 0)
    return BackfillSlice(snapshot_ids=snapshot_ids, remaining_estimate=remaining)


def count_pending(session: Session, *, tenant_id: str) -> int:
    """Count snapshots that still need their first tag pass.

    Drives the JobsView "Tagging…" banner (Phase 19.7).
    """
    from sqlalchemy import func  # noqa: PLC0415

    stmt = (
        select(func.count())
        .select_from(JobSnapshot)
        .where(JobSnapshot.tenant_id == tenant_id)
        .where(JobSnapshot.tags_status.in_((STATUS_PENDING, STATUS_COMPUTING)))
    )
    return int(session.execute(stmt).scalar() or 0)


__all__ = [
    "STATUS_COMPUTING",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_READY",
    "BackfillSlice",
    "TagWriteResult",
    "count_pending",
    "mark_pending",
    "select_stale_snapshots",
    "tag_snapshot",
]
