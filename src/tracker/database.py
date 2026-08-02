"""Application tracking database operations.

Bridges the in-memory state machine to persistent Application records.
Handles creating, updating, and querying application records.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.models import Application, Job, JobPosting, JobSnapshot
from src.core.state_machine import ApplicationState, AppStatus

logger = logging.getLogger("autoapply.tracker.database")


@dataclass(frozen=True)
class ApplicationJobView:
    """Read model shared by canonical and legacy application rows."""

    id: uuid.UUID
    source: str | None
    source_id: str | None
    company: str
    title: str
    location: str | None
    employment_type: str | None
    seniority: str | None
    description: str | None
    requirements: dict | None
    visa_sponsorship: bool | None
    ats_type: str | None
    application_url: str | None
    raw_data: dict | None


def _canonical_job_view(
    posting: JobPosting,
    snapshot: JobSnapshot | None,
) -> ApplicationJobView:
    return ApplicationJobView(
        id=posting.id,
        source=posting.source,
        source_id=posting.source_id,
        company=posting.company,
        title=snapshot.title if snapshot is not None else "",
        location=snapshot.location if snapshot is not None else None,
        employment_type=snapshot.employment_type if snapshot is not None else None,
        seniority=snapshot.seniority if snapshot is not None else None,
        description=snapshot.description if snapshot is not None else None,
        requirements=snapshot.requirements if snapshot is not None else None,
        visa_sponsorship=None,
        ats_type=posting.source,
        application_url=(
            snapshot.application_url if snapshot is not None else posting.canonical_url
        ),
        raw_data=snapshot.raw_data if snapshot is not None else None,
    )

def _orphan_job_view(application: Application) -> ApplicationJobView:
    """Keep an Application visible when both referenced job rows are gone."""
    missing_id = application.job_posting_id or application.job_id or application.id
    return ApplicationJobView(
        id=missing_id,
        source=None,
        source_id=None,
        company="Unknown company",
        title="Unavailable job",
        location=None,
        employment_type=None,
        seniority=None,
        description=None,
        requirements=None,
        visa_sponsorship=None,
        ats_type="orphaned",
        application_url=None,
        raw_data={
            "orphaned": True,
            "job_posting_id": str(application.job_posting_id)
            if application.job_posting_id
            else None,
            "legacy_job_id": str(application.job_id) if application.job_id else None,
        },
    )

def create_application(
    session: Session,
    job_id: uuid.UUID | None = None,
    match_score: float | None = None,
    resume_version: str | None = None,
    cover_letter_version: str | None = None,
    *,
    job_posting_id: uuid.UUID | None = None,
    job_snapshot_id: uuid.UUID | None = None,
    tenant_id: str = "default",
    submit_policy: str = "manual",
) -> Application:
    """Create an application with an explicit canonical or legacy binding."""
    if job_posting_id is None and job_id is None:
        raise ValueError("job_posting_id or legacy job_id is required")
    app = Application(
        tenant_id=tenant_id,
        job_id=job_id,
        job_posting_id=job_posting_id,
        job_snapshot_id=job_snapshot_id,
        submit_policy=submit_policy,
        status=AppStatus.DISCOVERED,
        match_score=match_score,
        resume_version=resume_version,
        cover_letter_version=cover_letter_version,
    )
    session.add(app)
    session.flush()
    logger.info(
        "Created application %s for job_posting=%s legacy_job=%s",
        app.id,
        job_posting_id,
        job_id,
    )
    return app

def sync_state_to_db(
    session: Session,
    app_id: uuid.UUID,
    state: ApplicationState,
    result: dict[str, Any] | None = None,
) -> Application:
    """Update an Application record from the in-memory state machine.

    Call this after the execution layer completes (or fails) to persist
    the final state, audit history, and results.
    """
    app = session.get(Application, app_id)
    if app is None:
        raise ValueError(f"Application {app_id} not found")

    app.status = str(state.status)
    app.state_history = state.history
    app.error_log = state.metadata.get("error", app.error_log)

    if result:
        app.fields_filled = result.get("fields_filled", app.fields_filled)
        app.fields_total = result.get("fields_total", app.fields_total)
        app.files_uploaded = result.get("files_uploaded", app.files_uploaded)
        app.qa_responses = result.get("qa_responses", app.qa_responses)
        app.screenshot_paths = result.get("screenshot_paths", app.screenshot_paths)
        # Phase 18.5: persist per-field record so the Review UI can show
        # "we tried these fields with these values, this one failed
        # because ..." rather than just the integer counters.
        if "fill_details" in result:
            app.fill_details = result.get("fill_details") or []

    if state.status == AppStatus.SUBMITTED and app.submitted_at is None:
        app.submitted_at = datetime.now(UTC)

    session.flush()
    logger.info("Synced application %s -> %s", app_id, state.status)
    return app


def update_outcome(
    session: Session,
    app_id: uuid.UUID,
    outcome: str,
) -> Application:
    """Record an application outcome (rejected/oa/interview/offer).

    Args:
        session: DB session.
        app_id: Application UUID.
        outcome: One of: pending, rejected, oa, interview, offer.
    """
    valid_outcomes = {"pending", "rejected", "oa", "interview", "offer"}
    if outcome not in valid_outcomes:
        raise ValueError(f"Invalid outcome '{outcome}', must be one of {valid_outcomes}")

    app = session.get(Application, app_id)
    if app is None:
        raise ValueError(f"Application {app_id} not found")

    app.outcome = outcome
    app.outcome_updated_at = datetime.now(UTC)
    session.flush()
    logger.info("Updated application %s outcome -> %s", app_id, outcome)
    return app


def get_application(session: Session, app_id: uuid.UUID) -> Application | None:
    """Retrieve a single application by ID."""
    return session.get(Application, app_id)


def get_applications(
    session: Session,
    status: str | None = None,
    outcome: str | None = None,
    company: str | None = None,
    limit: int | None = 50,
    offset: int = 0,
) -> list[Application]:
    """Query applications, including canonical JobPosting-bound rows."""
    stmt = select(Application).order_by(Application.updated_at.desc())
    if company:
        company_expr = func.coalesce(JobPosting.company, Job.company)
        stmt = (
            stmt.outerjoin(
                JobPosting,
                Application.job_posting_id == JobPosting.id,
            )
            .outerjoin(Job, Application.job_id == Job.id)
            .where(company_expr.ilike(f"%{company}%"))
        )
    if status:
        stmt = stmt.where(Application.status == status)
    if outcome:
        if outcome == "pending":
            stmt = stmt.where(Application.outcome.is_(None))
        else:
            stmt = stmt.where(Application.outcome == outcome)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    return list(session.execute(stmt).scalars().all())


def get_application_with_job(
    session: Session,
    app_id: uuid.UUID,
) -> tuple[Application, ApplicationJobView | Job] | None:
    """Retrieve one application with its canonical-or-legacy job view."""
    rows = get_applications_with_jobs(session, limit=None, application_id=app_id)
    return rows[0] if rows else None


def get_applications_with_jobs(
    session: Session,
    status: str | None = None,
    outcome: str | None = None,
    company: str | None = None,
    limit: int | None = 50,
    offset: int = 0,
    include_soft_deleted: bool = False,
    application_id: uuid.UUID | None = None,
) -> list[tuple[Application, ApplicationJobView | Job]]:
    """Return applications with a unified Job read model.

    Canonical rows join ``JobPosting`` and the application-bound snapshot;
    historical rows fall back to the legacy ``jobs`` table.
    """
    snapshot_id = func.coalesce(
        Application.job_snapshot_id,
        JobPosting.latest_snapshot_id,
    )
    stmt = (
        select(Application, JobPosting, JobSnapshot, Job)
        .outerjoin(JobPosting, Application.job_posting_id == JobPosting.id)
        .outerjoin(JobSnapshot, JobSnapshot.id == snapshot_id)
        .outerjoin(Job, Application.job_id == Job.id)
        .order_by(Application.updated_at.desc())
    )
    if not include_soft_deleted:
        stmt = stmt.where(Application.deleted_at.is_(None))
    if application_id is not None:
        stmt = stmt.where(Application.id == application_id)
    if status:
        stmt = stmt.where(Application.status == status)
    if outcome:
        if outcome == "pending":
            stmt = stmt.where(Application.outcome.is_(None))
        else:
            stmt = stmt.where(Application.outcome == outcome)
    if company:
        stmt = stmt.where(
            func.coalesce(JobPosting.company, Job.company).ilike(f"%{company}%")
        )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)

    result: list[tuple[Application, ApplicationJobView | Job]] = []
    for application, posting, snapshot, legacy_job in session.execute(stmt).all():
        if posting is not None:
            result.append((application, _canonical_job_view(posting, snapshot)))
        elif legacy_job is not None:
            result.append((application, legacy_job))
        else:
            result.append((application, _orphan_job_view(application)))
    return result

def get_application_counts(session: Session) -> dict[str, int]:
    """Get counts of applications by status.

    Phase 18.4: soft-deleted rows are excluded so dashboard counters
    don't keep counting rows the operator already removed.
    """
    stmt = (
        select(Application.status, func.count())
        .where(Application.deleted_at.is_(None))
        .group_by(Application.status)
    )
    rows = session.execute(stmt).all()
    return {status: count for status, count in rows}


def get_outcome_counts(session: Session) -> dict[str, int]:
    """Get counts of applications by outcome (only submitted apps)."""
    stmt = (
        select(Application.outcome, func.count())
        .where(Application.status == AppStatus.SUBMITTED)
        .where(Application.deleted_at.is_(None))
        .group_by(Application.outcome)
    )
    rows = session.execute(stmt).all()
    return {outcome or "pending": count for outcome, count in rows}
