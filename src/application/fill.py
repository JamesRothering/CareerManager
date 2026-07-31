"""Canonical application.fill use case.

The worker boundary accepts only ``applications.id``. It resolves the bound
JobPosting/JobSnapshot, profile, and generated materials, executes the existing
Playwright ATS adapter with submission disabled, then persists the state-machine
result back onto the same Application row.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from src.core.state_machine import ApplicationState, AppStatus

logger = logging.getLogger("autoapply.application.fill")

FillExecutor = Callable[..., Awaitable[Any]]


def run_application_fill(
    *,
    application_id: str,
    tenant_id: str,
    headless: bool = True,
    executor: FillExecutor | None = None,
) -> dict[str, Any]:
    """Execute one persisted application through fill-to-review."""
    try:
        application_uuid = UUID(application_id)
    except (TypeError, ValueError):
        return {
            "task": "application.fill",
            "application_id": application_id,
            "status": "invalid_id",
        }

    from src.application.jobs import (
        _detect_ats_from_url,
        _execute_application,
        _job_to_raw_job,
        _load_profile,
        _raw_job_from_index_posting,
    )
    from src.core.database import get_session_factory
    from src.core.models import Application, Job, JobPosting, JobSnapshot
    from src.utils.parallelism import run_coroutine_safely

    factory = get_session_factory()
    with factory() as session:
        application = session.get(Application, application_uuid)
        if application is None or application.tenant_id != tenant_id:
            return {
                "task": "application.fill",
                "application_id": str(application_uuid),
                "status": "application_not_found",
            }
        if application.status in {AppStatus.REVIEW_REQUIRED, AppStatus.SUBMITTED}:
            return {
                "task": "application.fill",
                "application_id": str(application_uuid),
                "status": "already_completed",
                "application_status": str(application.status),
            }
        if application.status == AppStatus.FAILED:
            return {
                "task": "application.fill",
                "application_id": str(application_uuid),
                "status": "failed_terminal",
                "application_status": str(application.status),
                "error": application.error_log,
            }

        job_posting_id = getattr(application, "job_posting_id", None)
        posting = session.get(JobPosting, job_posting_id) if job_posting_id else None
        if posting is not None:
            if posting.tenant_id != tenant_id:
                return {
                    "task": "application.fill",
                    "application_id": str(application_uuid),
                    "status": "posting_not_found",
                }
            snapshot_id = application.job_snapshot_id or posting.latest_snapshot_id
            snapshot = session.get(JobSnapshot, snapshot_id) if snapshot_id else None
            if (
                snapshot is None
                or snapshot.tenant_id != tenant_id
                or snapshot.posting_id != posting.id
            ):
                return {
                    "task": "application.fill",
                    "application_id": str(application_uuid),
                    "status": "snapshot_not_found",
                }
            job = _raw_job_from_index_posting(posting, snapshot)
        else:
            legacy_job = session.get(Job, application.job_id) if application.job_id else None
            if legacy_job is None:
                return {
                    "task": "application.fill",
                    "application_id": str(application_uuid),
                    "status": "posting_not_found",
                }
            job = _job_to_raw_job(legacy_job)

        resume_path = Path(application.resume_version) if application.resume_version else None
        cover_letter_path = (
            Path(application.cover_letter_version)
            if application.cover_letter_version
            else None
        )
        qa_responses = dict(application.qa_responses or {}) or None
        profile_id = getattr(application, "profile_id", None) or "default"
        initial_status = _coerce_fill_start_status(application.status)

    if resume_path is None or not resume_path.exists():
        return {
            "task": "application.fill",
            "application_id": str(application_uuid),
            "status": "materials_missing",
            "detail": "A generated resume is required before form fill.",
        }
    if cover_letter_path is not None and not cover_letter_path.exists():
        logger.warning(
            "application.fill cover letter path is missing for %s: %s",
            application_uuid,
            cover_letter_path,
        )
        cover_letter_path = None

    profile_data = _load_profile(profile_id) or _load_profile()
    if not profile_data:
        return {
            "task": "application.fill",
            "application_id": str(application_uuid),
            "status": "profile_not_found",
            "profile_id": profile_id,
        }
    application_url = job.application_url or ""
    ats_type = _detect_ats_from_url(application_url) or job.ats_type
    if not application_url:
        return {
            "task": "application.fill",
            "application_id": str(application_uuid),
            "status": "application_url_missing",
        }
    if not ats_type or ats_type == "unknown":
        ats_type = "company_site"

    state = ApplicationState(str(application_uuid), status=initial_status)
    execute = executor or _execute_application
    try:
        result = run_coroutine_safely(
            execute(
                url=application_url,
                ats_type=ats_type,
                job=job,
                profile_data=profile_data,
                resume_path=resume_path,
                cover_letter_path=cover_letter_path,
                qa_responses=qa_responses,
                # A fill task never clicks final submit. Human approval and
                # application.submit own that separate policy boundary.
                auto_submit=False,
                headless=headless,
                state=state,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- persist worker failures
        error = f"{type(exc).__name__}: {exc}"
        if state.is_active:
            state.fail(error)
        _persist_fill_result(
            factory=factory,
            application_uuid=application_uuid,
            state=state,
            result=None,
            qa_responses=qa_responses,
            error=error,
        )
        return {
            "task": "application.fill",
            "application_id": str(application_uuid),
            "status": "failed",
            "application_status": str(state.status),
            "error": error,
        }

    persisted = _persist_fill_result(
        factory=factory,
        application_uuid=application_uuid,
        state=state,
        result=result,
        qa_responses=qa_responses,
    )
    application_status = str(getattr(result, "status", state.status))
    status = (
        "review_required"
        if application_status == str(AppStatus.REVIEW_REQUIRED)
        else "failed"
        if application_status == str(AppStatus.FAILED)
        else "completed"
    )
    return {
        "task": "application.fill",
        "application_id": str(application_uuid),
        "status": status,
        "application_status": application_status,
        "fields_filled": persisted["fields_filled"],
        "fields_total": persisted["fields_total"],
        "fill_details": persisted["fill_details"],
        "files_uploaded": persisted["files_uploaded"],
        "qa_answered": persisted["qa_answered"],
        "screenshots": persisted["screenshot_paths"],
        "error": getattr(result, "error", None),
    }


def _coerce_fill_start_status(value: str | AppStatus) -> AppStatus:
    try:
        status = AppStatus(str(value))
    except ValueError:
        return AppStatus.MATERIALS_READY
    if status in {
        AppStatus.DISCOVERED,
        AppStatus.QUALIFIED,
        AppStatus.MATERIALS_READY,
        AppStatus.NEEDS_RETRY,
    }:
        return status
    return AppStatus.MATERIALS_READY


def _persist_fill_result(
    *,
    factory,
    application_uuid: UUID,
    state: ApplicationState,
    result,
    qa_responses: dict[str, str] | None,
    error: str | None = None,
) -> dict[str, Any]:
    from src.tracker.database import sync_state_to_db

    payload = {
        "fields_filled": int(getattr(result, "fields_filled", 0) or 0),
        "fields_total": int(getattr(result, "fields_total", 0) or 0),
        "fill_details": list(getattr(result, "fill_details", []) or []),
        "files_uploaded": list(getattr(result, "files_uploaded", []) or []),
        "qa_responses": qa_responses,
        "qa_answered": int(getattr(result, "qa_answered", 0) or 0),
        "screenshot_paths": [
            str(path) for path in (getattr(result, "screenshots", []) or [])
        ],
    }
    with factory() as session, session.begin():
        application = sync_state_to_db(
            session,
            application_uuid,
            state,
            payload,
        )
        if error:
            application.error_log = error
    return payload


__all__ = ["run_application_fill"]
