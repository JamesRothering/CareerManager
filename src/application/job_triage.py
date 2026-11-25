"""US-2.4: persist Pursue / Skip / Later and list by status."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.core.models import TENANT_DEFAULT, JobTriage

VALID_STATUSES = frozenset({"pursue", "skip", "later"})
VALID_FILTERS = frozenset({"all", "pursue", "skip", "later"})

_SNAPSHOT_FIELDS = (
    "id",
    "source",
    "source_id",
    "company",
    "title",
    "location",
    "application_url",
    "description",
)


class JobTriageError(ValueError):
    """Status or list filter was not pursue / skip / later."""


def job_triage_key(job: dict[str, Any] | None, job_key: str | None = None) -> str:
    explicit = str(job_key or "").strip()
    if explicit:
        return explicit[:400]
    payload = job if isinstance(job, dict) else {}
    source = str(payload.get("source") or "").strip()
    source_id = str(payload.get("source_id") or payload.get("id") or "").strip()
    if source and source_id:
        return f"{source}::{source_id}"[:400]
    return source_id[:400]


def _snapshot(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    snap = {field: job.get(field) for field in _SNAPSHOT_FIELDS if job.get(field) not in (None, "")}
    return snap or None


def set_job_triage(
    session: Session,
    *,
    status: str,
    job: dict[str, Any] | None = None,
    job_key: str | None = None,
    tenant_id: str = TENANT_DEFAULT,
    commit: bool = True,
) -> dict[str, Any]:
    value = (status or "").strip().lower()
    if value not in VALID_STATUSES:
        raise JobTriageError(status)
    key = job_triage_key(job, job_key)
    if not key:
        raise JobTriageError("job_key")

    tenant = (tenant_id or TENANT_DEFAULT).strip() or TENANT_DEFAULT
    now = datetime.now(UTC)
    snap = _snapshot(job)
    row = (
        session.query(JobTriage)
        .filter_by(tenant_id=tenant, job_key=key)
        .one_or_none()
    )
    if row is None:
        row = JobTriage(
            tenant_id=tenant,
            job_key=key,
            status=value,
            job=snap,
            decided_at=now,
        )
        session.add(row)
    else:
        row.status = value
        row.decided_at = now
        if snap:
            row.job = snap

    if commit:
        session.commit()
        session.refresh(row)

    return {
        "ok": True,
        "job_key": row.job_key,
        "status": row.status,
        "job": row.job,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


def list_job_triage(
    session: Session,
    *,
    tenant_id: str = TENANT_DEFAULT,
    status: str | None = None,
) -> dict[str, Any]:
    filter_key = (status or "all").strip().lower() or "all"
    if filter_key not in VALID_FILTERS:
        raise JobTriageError(status)

    tenant = (tenant_id or TENANT_DEFAULT).strip() or TENANT_DEFAULT
    rows = (
        session.query(JobTriage)
        .filter_by(tenant_id=tenant)
        .order_by(JobTriage.decided_at.desc())
        .all()
    )
    counts = {"pursue": 0, "skip": 0, "later": 0, "all": 0}
    items: list[dict[str, Any]] = []
    for row in rows:
        counts["all"] += 1
        counts[row.status] = counts.get(row.status, 0) + 1
        if filter_key != "all" and row.status != filter_key:
            continue
        items.append(
            {
                "job_key": row.job_key,
                "status": row.status,
                "job": row.job,
                "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            }
        )
    return {
        "ok": True,
        "status": filter_key,
        "items": items,
        "counts": counts,
    }
