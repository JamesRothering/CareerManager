"""US-3.1: log and list communications against a job."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.core.models import TENANT_DEFAULT, JobCommunication

VALID_CHANNELS = frozenset({"email", "phone", "linkedin", "other"})
VALID_DIRECTIONS = frozenset({"inbound", "outbound"})


class JobCommunicationError(ValueError):
    """Missing job id or an unknown channel/direction."""


def _serialize(row: JobCommunication) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "job_id": row.job_id,
        "channel": row.channel,
        "direction": row.direction,
        "summary": row.summary,
        "next_action": row.next_action or "",
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def log_job_communication(
    session: Session,
    *,
    job_id: str,
    channel: str,
    direction: str,
    summary: str,
    next_action: str = "",
    occurred_at: datetime | None = None,
    tenant_id: str = TENANT_DEFAULT,
    commit: bool = True,
) -> dict[str, Any]:
    job_key = str(job_id or "").strip()
    if not job_key:
        raise JobCommunicationError("job_id")
    channel_value = (channel or "").strip().lower()
    if channel_value not in VALID_CHANNELS:
        raise JobCommunicationError("channel")
    direction_value = (direction or "").strip().lower()
    if direction_value not in VALID_DIRECTIONS:
        raise JobCommunicationError("direction")
    text = str(summary or "").strip()
    if not text:
        raise JobCommunicationError("summary")

    tenant = (tenant_id or TENANT_DEFAULT).strip() or TENANT_DEFAULT
    when = occurred_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    row = JobCommunication(
        tenant_id=tenant,
        job_id=job_key,
        channel=channel_value,
        direction=direction_value,
        summary=text,
        next_action=str(next_action or "").strip(),
        occurred_at=when,
    )
    session.add(row)
    if commit:
        session.commit()
        session.refresh(row)
    return {"ok": True, "item": _serialize(row)}


def list_job_communications(
    session: Session,
    *,
    job_id: str,
    tenant_id: str = TENANT_DEFAULT,
) -> dict[str, Any]:
    job_key = str(job_id or "").strip()
    if not job_key:
        raise JobCommunicationError("job_id")
    tenant = (tenant_id or TENANT_DEFAULT).strip() or TENANT_DEFAULT
    rows = (
        session.query(JobCommunication)
        .filter_by(tenant_id=tenant, job_id=job_key)
        .order_by(JobCommunication.occurred_at.asc(), JobCommunication.created_at.asc())
        .all()
    )
    return {
        "ok": True,
        "job_id": job_key,
        "items": [_serialize(row) for row in rows],
    }
