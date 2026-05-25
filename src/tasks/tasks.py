"""Phase 14.6 task kinds, Phase 18.1 worker stub closeout.

Each task is a thin Celery wrapper over an existing application-layer
function. The wrapper's job is to:

* Validate the payload via a Pydantic model (so a malformed enqueue
  is rejected at the boundary, not deep inside a generator).
* Push the tenant context into the ContextVar (handled by
  :class:`AutoApplyTask.before_start`).
* Short-circuit via the idempotency key when applicable.
* Let Celery own retry / backoff / ack-late semantics.

Phase 18.1 replaced the fake-success ``status="scheduled"`` /
``status="stubbed"`` returns with real call chains:

* ``materials.generate`` invokes :func:`generate_material_for_job`
  for each requested document_type, atomically writes artifacts via
  :mod:`src.maintenance.atomic`, and stitches the result back onto
  the Application + ReviewQueueEntry rows.
* ``jobs.enrich`` calls :func:`enrich_posting` against the latest
  stored snapshot so the Phase 19 content-changed listener chain
  fires when the underlying content has drifted.
* ``application.prepare`` walks the Application row's invariants and
  links the latest materials onto the matching ReviewQueueEntry.
* ``application.fill`` / ``maintenance.status_sync`` return explicit
  ``status="not_implemented"`` because their browser / outcome-sync
  implementations sit behind a later phase; the names + payload
  contract stay so callers don't get a registration error.
* ``application.submit`` runs the Phase 17.5 pre-submit gate
  (``should_refresh(..., "before_submit")``) and parks the row at
  ``waiting_human`` via :mod:`src.tasks.gate` if the gate refuses,
  or returns ``not_implemented`` on the actual click-submit step.
* ``maintenance.gate_expire_sweep`` flips ``gate_queue`` rows past
  their TTL to ``expired``.
* ``maintenance.jd_health_check`` walks ``job_postings`` and applies
  :func:`project_by_time` so freshness decays without a manual nudge.
* ``maintenance.linkedin_cookie_refresh`` probes the LinkedIn
  session and records pass/fail in the audit row.
* ``maintenance.cache_eviction`` (Phase 18.4) drives the artifact
  cleanup + quarantine pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.tasks.app import celery_app
from src.tasks.base import AutoApplyTask

# Phase 19.3: ``src.jobs.enrich`` installs the production
# ``on_content_changed`` listener at module load (idempotent), so any
# entry point that imports it -- worker, web search, CLI -- enqueues
# ``posting.tag`` automatically.

logger = logging.getLogger(__name__)


# ---- Payload schemas -------------------------------------------------


class SearchRefreshPayload(BaseModel):
    """Phase 19.3b: ``profile_id`` resolves a saved-search definition
    from :mod:`src.application.search_profiles`; ``query_id`` is kept
    for the legacy "re-run this specific normalized search" entrypoint.

    At least one of the two must be set; the daily fan-out enqueues by
    ``profile_id`` so the upstream rule (every search hits upstream)
    holds while we remove the no-op task body.
    """

    query_id: str | None = None
    profile_id: str | None = None
    source: str = Field(default="linkedin")
    max_pages: int | None = None


class JobEnrichPayload(BaseModel):
    posting_id: str


class PostingTagPayload(BaseModel):
    """Phase 19.3: re-tag a single snapshot."""

    snapshot_id: str


class PostingTagBackfillPayload(BaseModel):
    """Phase 19.3: paginated retag for snapshots trailing ``TAGGER_VERSION``."""

    batch_size: int = 100
    max_batches: int = 1


class MaterialsGeneratePayload(BaseModel):
    job_id: str
    # Phase 18.2: optional inline job dict for callers (e.g. the
    # JobsView "Generate" button) that have search-result data but
    # haven't persisted a JobPosting yet. When present, the task body
    # uses this payload directly and skips the JobPosting / JobSnapshot
    # lookup. ``job_id`` is still required so the audit row carries a
    # stable identifier.
    job: dict[str, Any] | None = None
    # Phase 18.2: when set, the regenerate flow asks the worker to
    # write the produced artifact paths back onto the Application
    # row so the kanban / outcome views see the new draft without
    # the operator having to re-poll.
    application_id: str | None = None
    profile_id: str | None = None
    template_id: str | None = None
    document_types: list[str] = Field(default_factory=lambda: ["resume", "cover_letter"])
    # Phase 17.8 / 18.x: per-call overrides for the materials router.
    # Empty / None means "let resolve_material_choice fall back to the
    # user's Settings → Default material strategy".
    resume_strategy: str | None = None
    resume_template_id: str | None = None
    resume_source_document_id: str | None = None
    resume_patch_aggressiveness: str | None = None
    resume_patch_allow_reorder_sections: bool | None = None
    resume_patch_allow_add_remove_bullets: bool | None = None
    cover_letter_strategy: str | None = None
    cover_letter_template_id: str | None = None
    cover_letter_source_document_id: str | None = None
    cover_letter_patch_aggressiveness: str | None = None
    cover_letter_patch_allow_reorder_sections: bool | None = None
    cover_letter_patch_allow_add_remove_bullets: bool | None = None


class ApplicationPreparePayload(BaseModel):
    application_id: str


class ApplicationFillPayload(BaseModel):
    application_id: str


class ApplicationSubmitPayload(BaseModel):
    application_id: str


class OrchestrationPlanRunPayload(BaseModel):
    """Phase 17.1 plan_run task payload."""

    automation_plan_id: str | None = None
    automation_plan_name: str | None = None
    profile_id: str = "default"
    search_profile_id: str | None = None
    top_n: int = 10
    dry_run: bool = False
    auto_submit: bool = False
    skip_previously_applied: bool = True
    scrape_enabled: bool = True
    resume_strategy: str | None = None
    resume_template_id: str | None = None
    resume_source_document_id: str | None = None
    resume_patch_aggressiveness: str | None = None
    resume_patch_allow_reorder_sections: bool | None = None
    resume_patch_allow_add_remove_bullets: bool | None = None
    cover_letter_strategy: str | None = None
    cover_letter_template_id: str | None = None
    cover_letter_source_document_id: str | None = None
    cover_letter_patch_aggressiveness: str | None = None
    cover_letter_patch_allow_reorder_sections: bool | None = None
    cover_letter_patch_allow_add_remove_bullets: bool | None = None


class StatusSyncPayload(BaseModel):
    """Empty by default -- the scheduled status_sync sweeps every
    in-flight application; a one-off CLI invocation can pass
    ``application_id`` to scope it."""

    application_id: str | None = None


def _coerce(model_cls: type[BaseModel], data: dict[str, Any] | None) -> BaseModel:
    try:
        return model_cls(**(data or {}))
    except ValidationError as exc:
        # Raise a TypeError so Celery's retry layer treats it as a
        # *terminal* failure (not transient); a malformed payload will
        # not get better on retry.
        raise TypeError(f"invalid {model_cls.__name__}: {exc}") from exc


# ---- Tasks: search ---------------------------------------------------


@celery_app.task(name="search.refresh", base=AutoApplyTask, bind=True)
def search_refresh(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Re-scrape one saved-search profile (Phase 19.3b).

    Resolves the saved-search definition from
    :mod:`src.application.search_profiles` and invokes
    :func:`src.application.jobs.search_jobs` with ``use_job_index=True``
    so the normal ``cached_search`` flow runs and ``search_results`` rows
    are kept current. ``force_refresh=True`` is implicit: the Phase 19
    rule is that every saved-search tick hits upstream.

    ``query_id`` is still accepted for the legacy single-query refresh
    path (operator UI "refresh this search now" button); the body looks
    up the ``SearchQuery`` row, rebuilds its parameters from
    ``raw_params``, and dispatches the same way.
    """
    args = _coerce(SearchRefreshPayload, payload)
    if not args.profile_id and not args.query_id:
        raise TypeError(
            "search.refresh requires either profile_id or query_id"
        )

    logger.info(
        "search.refresh profile_id=%s query_id=%s source=%s",
        args.profile_id,
        args.query_id,
        args.source,
    )

    import asyncio  # noqa: PLC0415

    from src.application.jobs import search_jobs as search_jobs_use_case  # noqa: PLC0415
    from src.application.search_profiles import (  # noqa: PLC0415
        load_search_profiles_data,
    )

    profile_payload: dict[str, Any] | None = None
    if args.profile_id:
        data = load_search_profiles_data()
        profiles = {p["id"]: p for p in data.get("profiles", [])}
        profile_payload = profiles.get(args.profile_id)
        if profile_payload is None:
            return {
                "task": "search.refresh",
                "profile_id": args.profile_id,
                "status": "profile_not_found",
            }
    else:
        # query_id branch: look the row up and reuse its raw_params.
        from uuid import UUID  # noqa: PLC0415

        from src.core.database import get_session_factory  # noqa: PLC0415
        from src.core.models import SearchQuery  # noqa: PLC0415

        try:
            query_uuid = UUID(args.query_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {
                "task": "search.refresh",
                "query_id": args.query_id,
                "status": "invalid_query_id",
            }
        factory = get_session_factory()
        with factory() as session:
            row = session.get(SearchQuery, query_uuid)
            if row is None:
                return {
                    "task": "search.refresh",
                    "query_id": args.query_id,
                    "status": "query_not_found",
                }
            profile_payload = {
                "source": row.source,
                **(row.raw_params or {}),
            }

    assert profile_payload is not None  # noqa: S101 -- guarded above

    profile_id_for_search = args.profile_id or "default"
    try:
        result = asyncio.run(
            search_jobs_use_case(
                profile=profile_id_for_search,
                source=profile_payload.get("source") or args.source or "ats",
                ats=profile_payload.get("ats") or None,
                company=profile_payload.get("company") or None,
                keyword=None,
                keywords=profile_payload.get("keywords") or None,
                search_location=None,
                time_filter=profile_payload.get("time_filter") or "week",
                experience_levels=profile_payload.get("experience_levels") or None,
                employment_types=profile_payload.get("employment_types") or None,
                location_types=profile_payload.get("location_types") or None,
                locations=profile_payload.get("locations") or None,
                pay_operator=profile_payload.get("pay_operator") or None,
                pay_amount=profile_payload.get("pay_amount"),
                experience_operator=profile_payload.get("experience_operator") or None,
                experience_years=profile_payload.get("experience_years"),
                education_levels=profile_payload.get("education_levels") or None,
                max_pages=args.max_pages or profile_payload.get("max_pages") or 20,
                no_enrich=False,
                headless=True,
                require_keyword_for_linkedin=False,
                use_job_index=True,
                force_refresh=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("search.refresh failed for profile_id=%s", profile_id_for_search)
        return {
            "task": "search.refresh",
            "profile_id": args.profile_id,
            "query_id": args.query_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    counts = result.get("counts") or {}
    return {
        "task": "search.refresh",
        "profile_id": args.profile_id,
        "query_id": args.query_id,
        "source": profile_payload.get("source") or args.source,
        "total": counts.get("total"),
        "errors": result.get("errors") or [],
        "status": "ok",
    }


@celery_app.task(name="search.daily_fanout", base=AutoApplyTask, bind=True)
def search_daily_fanout(self: AutoApplyTask) -> dict[str, Any]:
    """Phase 19.3b: enumerate saved-search profiles and enqueue one
    ``search.refresh`` child per active profile.

    The Phase 19 rule is that every saved-search tick hits upstream:
    this fan-out simply explodes the schedule entry into per-profile
    refreshes; the children themselves call ``cached_search`` with
    ``force_refresh=True`` so the cached-result short-circuit cannot
    hide newly posted jobs.
    """
    logger.info("search.daily_fanout tick")

    from src.application.search_profiles import (  # noqa: PLC0415
        load_search_profiles_data,
    )

    data = load_search_profiles_data()
    profiles = data.get("profiles") or []

    enqueued: list[dict[str, str]] = []
    for profile in profiles:
        profile_id = profile.get("id")
        if not profile_id:
            continue
        try:
            async_result = celery_app.send_task(
                "search.refresh",
                kwargs={
                    "profile_id": profile_id,
                    "source": profile.get("source") or "ats",
                    "max_pages": profile.get("max_pages"),
                },
                queue="search",
            )
        except Exception as exc:  # noqa: BLE001 -- one bad profile must not stop the rest
            logger.warning(
                "search.daily_fanout: enqueue failed for profile %s: %s",
                profile_id,
                exc,
            )
            continue
        enqueued.append(
            {
                "profile_id": profile_id,
                "task_id": str(getattr(async_result, "id", "")),
            }
        )

    return {
        "task": "search.daily_fanout",
        "enqueued": enqueued,
        "profile_count": len(profiles),
        "status": "ok",
    }


# ---- Tasks: orchestration --------------------------------------------


@celery_app.task(name="notifications.morning_digest", base=AutoApplyTask, bind=True)
def notifications_morning_digest(self: AutoApplyTask) -> dict[str, Any]:
    """Phase 17.6: 08:00 morning digest tick."""
    import asyncio  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.orchestration.digest import compute_digest  # noqa: PLC0415
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    del asyncio  # not currently needed; placeholder if we go async

    tenant_id = current_tenant_id() or "default"
    factory = get_session_factory()
    with factory() as session:
        payload = compute_digest(session, tenant_id=tenant_id)
    logger.info("morning_digest tenant=%s: %s", tenant_id, payload.headline)
    return payload.to_dict()


@celery_app.task(name="orchestration.plan_run", base=AutoApplyTask, bind=True)
def orchestration_plan_run(
    self: AutoApplyTask, **payload: Any
) -> dict[str, Any]:
    """Phase 17.1: top-level batch application automation task."""
    args = _coerce(OrchestrationPlanRunPayload, payload)
    import asyncio  # noqa: PLC0415

    from src.orchestration.plan_run import run_plan  # noqa: PLC0415
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"
    logger.info(
        "orchestration.plan_run starting tenant=%s profile=%s top_n=%d dry_run=%s",
        tenant_id,
        args.profile_id,
        args.top_n,
        args.dry_run,
    )
    report = asyncio.run(
        run_plan(
            tenant_id=tenant_id,
            profile_id=args.profile_id,
            search_profile_id=args.search_profile_id,
            top_n=args.top_n,
            dry_run=args.dry_run,
            auto_submit=args.auto_submit,
            skip_previously_applied=args.skip_previously_applied,
            scrape_enabled=args.scrape_enabled,
            resume_strategy=args.resume_strategy,
            resume_template_id=args.resume_template_id,
            resume_source_document_id=args.resume_source_document_id,
            resume_patch_aggressiveness=args.resume_patch_aggressiveness,
            resume_patch_allow_reorder_sections=args.resume_patch_allow_reorder_sections,
            resume_patch_allow_add_remove_bullets=args.resume_patch_allow_add_remove_bullets,
            cover_letter_strategy=args.cover_letter_strategy,
            cover_letter_template_id=args.cover_letter_template_id,
            cover_letter_source_document_id=args.cover_letter_source_document_id,
            cover_letter_patch_aggressiveness=args.cover_letter_patch_aggressiveness,
            cover_letter_patch_allow_reorder_sections=args.cover_letter_patch_allow_reorder_sections,
            cover_letter_patch_allow_add_remove_bullets=args.cover_letter_patch_allow_add_remove_bullets,
        )
    )
    try:
        from src.orchestration.digest import persist_plan_run_report  # noqa: PLC0415

        persist_plan_run_report(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_run: report persistence failed: %s", exc)

    return report.to_dict()


# ---- Tasks: jobs -----------------------------------------------------


@celery_app.task(name="jobs.enrich", base=AutoApplyTask, bind=True)
def jobs_enrich(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Re-feed the latest stored snapshot through :func:`enrich_posting`.

    Phase 18.1: the task previously logged + returned ``scheduled``.
    It now does a real call into ``src.jobs.enrich.enrich_posting``
    against the latest JobSnapshot for ``posting_id``. The function
    is idempotent (same content -> existing snapshot, content
    changed -> new snapshot + ``ContentChangedEvent``), which is
    what the Phase 19 ``posting.tag`` listener chain needs.

    The task body intentionally does NOT do a network scrape -- the
    LinkedIn / ATS scrape paths require Playwright and are owned by
    the orchestrator's search step. ``jobs.enrich`` is the
    refresh-and-snapshot primitive that runs after fresh content is
    available.
    """
    args = _coerce(JobEnrichPayload, payload)
    logger.info("jobs.enrich posting_id=%s", args.posting_id)

    from uuid import UUID  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import JobPosting, JobSnapshot  # noqa: PLC0415
    from src.jobs.enrich import enrich_posting  # noqa: PLC0415
    from src.jobs.store import JobIndexStore  # noqa: PLC0415

    try:
        posting_uuid = UUID(args.posting_id)
    except ValueError:
        return {
            "task": "jobs.enrich",
            "posting_id": args.posting_id,
            "status": "invalid_posting_id",
        }

    factory = get_session_factory()
    with factory() as session, session.begin():
        posting = session.get(JobPosting, posting_uuid)
        if posting is None:
            return {
                "task": "jobs.enrich",
                "posting_id": args.posting_id,
                "status": "posting_not_found",
            }
        if posting.latest_snapshot_id is None:
            return {
                "task": "jobs.enrich",
                "posting_id": args.posting_id,
                "status": "no_snapshot",
                "detail": "Posting has no JobSnapshot yet; a search/scrape must run first.",
            }
        snapshot = session.get(JobSnapshot, posting.latest_snapshot_id)
        if snapshot is None:
            return {
                "task": "jobs.enrich",
                "posting_id": args.posting_id,
                "status": "snapshot_missing",
            }

        content = {
            "title": snapshot.title,
            "location": snapshot.location,
            "employment_type": snapshot.employment_type,
            "seniority": snapshot.seniority,
            "description": snapshot.description,
            "requirements": snapshot.requirements,
            "application_url": snapshot.application_url,
            "raw_data": snapshot.raw_data,
        }
        store = JobIndexStore(session)
        result = enrich_posting(
            store=store,
            source=posting.source,
            source_id=posting.source_id,
            company=posting.company,
            content=content,
        )

    return {
        "task": "jobs.enrich",
        "posting_id": str(posting_uuid),
        "snapshot_id": str(result.snapshot_id),
        "content_changed": result.content_changed,
        "state": result.state,
        "status": "ok",
    }


# ---- Tasks: posting tagging (Phase 19.3) ----------------------------


@celery_app.task(name="posting.tag", base=AutoApplyTask, bind=True)
def posting_tag(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Compute + persist A1 objective tags for one ``JobSnapshot``.

    Fired by the ``on_content_changed`` listener (Phase 19.3) whenever
    ``enrich_posting`` lands a new snapshot, and also as the per-row
    unit of work for ``posting.tag_backfill``. The tagger is pure-
    function and profile-independent; this wrapper only owns the DB
    transaction and audit envelope.
    """
    args = _coerce(PostingTagPayload, payload)
    logger.info("posting.tag snapshot_id=%s", args.snapshot_id)

    from uuid import UUID  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.jobs.tag_service import tag_snapshot  # noqa: PLC0415

    try:
        snapshot_uuid = UUID(args.snapshot_id)
    except ValueError:
        return {
            "task": "posting.tag",
            "snapshot_id": args.snapshot_id,
            "status": "invalid_snapshot_id",
        }

    factory = get_session_factory()
    with factory() as session, session.begin():
        try:
            result = tag_snapshot(session, snapshot_uuid)
        except LookupError:
            return {
                "task": "posting.tag",
                "snapshot_id": args.snapshot_id,
                "status": "snapshot_not_found",
            }

    return {
        "task": "posting.tag",
        "snapshot_id": str(result.snapshot_id),
        "posting_id": str(result.posting_id),
        "tags_status": result.status,
        "tagger_version": result.tagger_version,
        "tags": result.tags,
        "error": result.error,
        "status": "ok" if result.error is None else "failed",
    }


@celery_app.task(name="posting.tag_backfill", base=AutoApplyTask, bind=True)
def posting_tag_backfill(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Paginated retag for snapshots whose ``tagger_version`` trails
    :data:`TAGGER_VERSION`.

    Each batch is one transaction. The "tagging in progress" banner in
    JobsView (Phase 19.7) reads :func:`count_pending` independently;
    until the backfill drains, the filter fast-path falls back to slow
    scoring rather than mis-rejecting on stale tags (D029).
    """
    args = _coerce(PostingTagBackfillPayload, payload)
    logger.info(
        "posting.tag_backfill batch_size=%d max_batches=%d",
        args.batch_size,
        args.max_batches,
    )

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.jobs.tag_service import (  # noqa: PLC0415
        select_stale_snapshots,
        tag_snapshot,
    )
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"
    factory = get_session_factory()

    processed = 0
    failed = 0
    remaining = 0
    for _ in range(max(args.max_batches, 1)):
        with factory() as session, session.begin():
            slice_ = select_stale_snapshots(
                session, tenant_id=tenant_id, batch_size=args.batch_size
            )
            if not slice_.snapshot_ids:
                remaining = slice_.remaining_estimate
                break
            for snap_id in slice_.snapshot_ids:
                try:
                    result = tag_snapshot(session, snap_id)
                except LookupError:
                    continue
                processed += 1
                if result.status != "ready":
                    failed += 1
            remaining = max(slice_.remaining_estimate - len(slice_.snapshot_ids), 0)
        if processed == 0:
            break

    return {
        "task": "posting.tag_backfill",
        "tenant_id": tenant_id,
        "processed": processed,
        "failed": failed,
        "remaining_estimate": remaining,
        "batch_size": args.batch_size,
        "max_batches": args.max_batches,
        "status": "ok",
    }


# ---- Tasks: materials ------------------------------------------------


@celery_app.task(name="materials.generate", base=AutoApplyTask, bind=True)
def materials_generate(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Phase 18.1: full materials generation from a JobPosting.

    Loads the posting + its latest snapshot, builds the web-shaped
    job payload that :func:`generate_material_for_job` expects, then
    runs generation for each requested document_type. Artifact paths
    are written onto any matching :class:`ReviewQueueEntry` (so the
    review-queue kanban can render the produced files) and the
    structured artifact map is returned to the audit row.

    Phase 18.2 will surface the same map through
    ``GET /api/tasks/{task_id}`` via :class:`TaskRecord.result`; the
    18.1 body just returns the shape so that wire-up is a pure read.
    """
    args = _coerce(MaterialsGeneratePayload, payload)
    logger.info(
        "materials.generate job_id=%s document_types=%s",
        args.job_id,
        args.document_types,
    )

    import asyncio  # noqa: PLC0415
    from uuid import UUID  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from src.application.jobs import generate_material_for_job  # noqa: PLC0415
    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import (  # noqa: PLC0415
        JobPosting,
        JobSnapshot,
        ReviewQueueEntry,
    )
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"

    # Phase 18.2: if the caller passed an inline ``job`` dict (the
    # JobsView "Generate" button hands us scraped data that isn't
    # persisted yet), use it verbatim. Otherwise fall through to the
    # JobPosting + JobSnapshot lookup.
    job_payload: dict[str, Any] = {}
    job_uuid = None
    if isinstance(args.job, dict) and args.job:
        job_payload = dict(args.job)
    else:
        try:
            job_uuid = UUID(args.job_id)
        except ValueError:
            return {
                "task": "materials.generate",
                "job_id": args.job_id,
                "document_types": args.document_types,
                "status": "invalid_job_id",
            }

        factory = get_session_factory()
        with factory() as session:
            posting = session.get(JobPosting, job_uuid)
            if posting is None:
                return {
                    "task": "materials.generate",
                    "job_id": args.job_id,
                    "document_types": args.document_types,
                    "status": "posting_not_found",
                }
            snapshot = (
                session.get(JobSnapshot, posting.latest_snapshot_id)
                if posting.latest_snapshot_id
                else None
            )
            job_payload = {
                "id": str(posting.id),
                "source": posting.source,
                "source_id": posting.source_id,
                "company": posting.company,
                "title": snapshot.title if snapshot else "",
                "location": snapshot.location if snapshot else None,
                "employment_type": snapshot.employment_type if snapshot else None,
                "seniority": snapshot.seniority if snapshot else None,
                "description": snapshot.description if snapshot else None,
                "requirements": snapshot.requirements if snapshot else None,
                "application_url": snapshot.application_url if snapshot else None,
                "ats_type": posting.source,
                "raw_data": snapshot.raw_data if snapshot else None,
            }

    if not job_payload.get("title"):
        return {
            "task": "materials.generate",
            "job_id": args.job_id,
            "document_types": args.document_types,
            "status": "no_snapshot",
            "detail": "Posting has no JobSnapshot; run jobs.enrich first.",
        }

    document_types = args.document_types or ["resume", "cover_letter"]
    artifacts: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []

    def _resolve_overrides(doc_type: str) -> tuple[str, dict[str, Any]] | None:
        if doc_type.startswith("resume"):
            return "resume_docx", {
                "strategy": args.resume_strategy,
                "template_id": args.resume_template_id,
                "source_document_id": args.resume_source_document_id,
                "patch_aggressiveness": args.resume_patch_aggressiveness,
                "patch_allow_reorder_sections": args.resume_patch_allow_reorder_sections,
                "patch_allow_add_remove_bullets": args.resume_patch_allow_add_remove_bullets,
            }
        if doc_type.startswith("cover_letter"):
            return "cover_letter_docx", {
                "strategy": args.cover_letter_strategy,
                "template_id": args.cover_letter_template_id,
                "source_document_id": args.cover_letter_source_document_id,
                "patch_aggressiveness": args.cover_letter_patch_aggressiveness,
                "patch_allow_reorder_sections": args.cover_letter_patch_allow_reorder_sections,
                "patch_allow_add_remove_bullets": args.cover_letter_patch_allow_add_remove_bullets,
            }
        return None

    async def _generate_one(doc_type: str) -> tuple[str, dict[str, Any] | None, dict | None]:
        resolved = _resolve_overrides(doc_type)
        if resolved is None:
            return doc_type, None, {
                "document_type": doc_type,
                "error": "unknown_document_type",
            }
        material_type, overrides = resolved
        try:
            result = await generate_material_for_job(
                job_payload=job_payload,
                material_type=material_type,
                use_llm=False,
                template_id=overrides["template_id"],
                profile_id=args.profile_id,
                strategy=overrides["strategy"],
                source_document_id=overrides["source_document_id"],
                patch_aggressiveness=overrides["patch_aggressiveness"],
                patch_allow_reorder_sections=overrides["patch_allow_reorder_sections"],
                patch_allow_add_remove_bullets=overrides["patch_allow_add_remove_bullets"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("materials.generate: %s failed", material_type)
            return doc_type, None, {"document_type": doc_type, "error": str(exc)}

        if not result.get("ok"):
            return doc_type, None, {
                "document_type": doc_type,
                "error": result.get("error") or "generation_failed",
                "error_code": result.get("error_code") or "generation_failed",
            }
        artifact = result.get("artifact") or {}
        return (
            doc_type,
            {
                "material_type": result.get("material_type"),
                "path": artifact.get("path"),
                "filename": artifact.get("filename"),
                "artifacts": result.get("artifacts"),
                "strategy": result.get("strategy"),
                "strategy_notes": result.get("strategy_notes"),
            },
            None,
        )

    # Phase 18.5: resume + cover-letter for a single job run
    # concurrently via asyncio.gather. The two generators share the
    # same job + profile data but produce independent artifacts, so
    # the only contention is on the global / per-provider LLM gate
    # in :mod:`src.utils.parallelism` (which is the whole point of
    # running them in parallel -- the gate caps provider load while
    # local concurrency cuts wall-clock).
    async def _generate_all() -> None:
        results = await asyncio.gather(
            *[_generate_one(dt) for dt in document_types]
        )
        for doc_type, artifact_payload, error in results:
            if error is not None:
                errors.append(error)
            if artifact_payload is not None:
                artifacts[doc_type] = artifact_payload

    asyncio.run(_generate_all())

    # Link the produced files onto any pending review-queue entry for
    # this posting so the kanban can preview them without re-running
    # the generator. We pick the most recent pending entry by
    # created_at; there should normally be only one because of the
    # partial-unique-pending index.
    resume_path = (
        artifacts.get("resume", {}).get("path")
        or artifacts.get("resume_docx", {}).get("path")
    )
    cover_letter_path = (
        artifacts.get("cover_letter", {}).get("path")
        or artifacts.get("cover_letter_docx", {}).get("path")
    )
    application_updates: dict[str, str] = {}
    if artifacts and job_uuid is not None:
        try:
            factory = get_session_factory()
            with factory() as session, session.begin():
                stmt = (
                    select(ReviewQueueEntry)
                    .where(ReviewQueueEntry.tenant_id == tenant_id)
                    .where(ReviewQueueEntry.job_id == job_uuid)
                    .where(ReviewQueueEntry.status == "pending")
                    .order_by(ReviewQueueEntry.created_at.desc())
                    .limit(1)
                )
                row = session.execute(stmt).scalar_one_or_none()
                if row is not None and resume_path:
                    row.materials_path = resume_path
        except Exception as exc:  # noqa: BLE001 -- never bounce a successful generation
            logger.warning(
                "materials.generate: linking artifacts to review entry failed: %s", exc
            )

    # Phase 18.2: when the regenerate flow named an application, write
    # the new artifact paths back onto that row so the kanban + outcome
    # views see the new draft. We keep this best-effort so a partial
    # generation (resume succeeded, cover-letter failed) still updates
    # what it can.
    if args.application_id and artifacts:
        try:
            from src.core.models import Application  # noqa: PLC0415

            app_uuid = UUID(args.application_id)
            factory = get_session_factory()
            with factory() as session, session.begin():
                app_row = session.get(Application, app_uuid)
                if app_row is not None:
                    if resume_path:
                        app_row.resume_version = resume_path
                        application_updates["resume_version"] = resume_path
                    if cover_letter_path:
                        app_row.cover_letter_version = cover_letter_path
                        application_updates["cover_letter_version"] = cover_letter_path
        except (ValueError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "materials.generate: application writeback failed: %s", exc
            )

    return {
        "task": "materials.generate",
        "job_id": args.job_id,
        "document_types": document_types,
        "artifacts": artifacts,
        "errors": errors,
        "application_id": args.application_id,
        "application_updates": application_updates,
        "resume_path": resume_path,
        "cover_letter_path": cover_letter_path,
        "status": "ok" if not errors else ("partial" if artifacts else "failed"),
    }


# ---- Tasks: application ----------------------------------------------


@celery_app.task(name="application.prepare", base=AutoApplyTask, bind=True)
def application_prepare(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Phase 18.1: bind the latest materials onto the matching
    ReviewQueueEntry so the review kanban can render previews.

    The orchestrator already persists pending review entries; this
    task body is the seam between the materials task's artifacts and
    the kanban row. ``application_id`` is interpreted as either an
    ``applications.id`` or a ``job_postings.id`` (the orchestrator
    uses the latter today; tracking-app callers use the former).
    Missing rows are treated as a non-error ``not_found`` so a stale
    Beat enqueue doesn't bounce the queue.
    """
    args = _coerce(ApplicationPreparePayload, payload)
    logger.info("application.prepare application_id=%s", args.application_id)

    from uuid import UUID  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import Application, ReviewQueueEntry  # noqa: PLC0415
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"

    try:
        target_uuid = UUID(args.application_id)
    except ValueError:
        return {
            "task": "application.prepare",
            "application_id": args.application_id,
            "status": "invalid_id",
        }

    factory = get_session_factory()
    with factory() as session, session.begin():
        app = session.get(Application, target_uuid)
        entry_q = (
            select(ReviewQueueEntry)
            .where(ReviewQueueEntry.tenant_id == tenant_id)
            .where(ReviewQueueEntry.status == "pending")
        )
        if app is not None:
            entry_q = entry_q.where(ReviewQueueEntry.job_id == app.job_id)
        else:
            entry_q = entry_q.where(ReviewQueueEntry.job_id == target_uuid)
        entry = session.execute(
            entry_q.order_by(ReviewQueueEntry.created_at.desc()).limit(1)
        ).scalar_one_or_none()

        if app is not None and entry is not None:
            preferred = app.resume_version or app.cover_letter_version
            if preferred and not entry.materials_path:
                entry.materials_path = preferred

    return {
        "task": "application.prepare",
        "application_id": str(target_uuid),
        "review_entry_id": str(entry.id) if entry is not None else None,
        "application_status": app.status if app is not None else None,
        "materials_path": entry.materials_path if entry is not None else None,
        "status": "ok",
    }


@celery_app.task(name="application.fill", base=AutoApplyTask, bind=True)
def application_fill(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Phase 18.1: the form-fill leg of the application pipeline.

    The actual browser automation lives in :mod:`src.execution` and is
    driven from the agent loop, not from this Celery task -- we keep
    the task registered (so the Phase 14 audit + Phase 17.5 gate
    plumbing both work) but explicitly return ``not_implemented`` so
    the wire isn't a fake success.
    """
    args = _coerce(ApplicationFillPayload, payload)
    logger.info("application.fill application_id=%s", args.application_id)
    return {
        "task": "application.fill",
        "application_id": args.application_id,
        "status": "not_implemented",
        "detail": (
            "Browser-driven form-fill is owned by src.execution.form_filler "
            "and the agent loop; the task is registered for audit + Beat "
            "wiring but the worker body does not execute Playwright."
        ),
    }


@celery_app.task(name="application.submit", base=AutoApplyTask, bind=True)
def application_submit(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Phase 18.1: pre-submit gate + ``waiting_human`` parking.

    The pre-submit gate (Phase 17.5) re-runs
    ``should_refresh(..., "before_submit")``. If the JD is stale,
    the task opens a HITL :class:`GateRequest` and parks the task at
    ``waiting_human`` so the operator decides whether to refresh and
    proceed. If the gate passes we still return ``not_implemented``
    on the actual click-submit step (that's the browser path owned by
    :mod:`src.execution`); the audit row records the gate verdict and
    artifacts so the failure mode is "explicit hand-off", not
    "submitted silently".
    """
    args = _coerce(ApplicationSubmitPayload, payload)
    logger.info("application.submit application_id=%s", args.application_id)

    from uuid import UUID  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import Application, JobPosting, TaskRecord  # noqa: PLC0415
    from src.jobs.freshness import should_refresh  # noqa: PLC0415
    from src.tasks import gate  # noqa: PLC0415
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"

    try:
        application_uuid = UUID(args.application_id)
    except ValueError:
        return {
            "task": "application.submit",
            "application_id": args.application_id,
            "status": "invalid_id",
        }

    factory = get_session_factory()
    with factory() as session, session.begin():
        app = session.get(Application, application_uuid)
        if app is None:
            return {
                "task": "application.submit",
                "application_id": str(application_uuid),
                "status": "application_not_found",
            }
        posting = session.get(JobPosting, app.job_id) if app.job_id else None
        verdict = (
            should_refresh(posting, context="before_submit")
            if posting is not None
            else None
        )

        gate_id: str | None = None
        if verdict is not None and verdict.should_refresh:
            # Park at waiting_human via the Postgres-backed gate. The
            # operator's approve/reject re-enqueues a follow-up task
            # under the same idempotency key (Phase 14.4 contract).
            celery_task_id = getattr(self.request, "id", None)
            task_row = None
            if celery_task_id:
                from src.tasks.audit import find_by_celery_id  # noqa: PLC0415

                task_row = find_by_celery_id(session, celery_task_id)
            gate_row = gate.open_request(
                session,
                kind="application.submit:freshness",
                summary=(
                    f"Pre-submit gate refused for application {application_uuid}: "
                    f"{verdict.reason}"
                ),
                payload={
                    "application_id": str(application_uuid),
                    "reason": verdict.reason,
                    "age_hours": verdict.age_hours,
                    "budget_hours": verdict.budget_hours,
                },
                task_id=task_row.id if isinstance(task_row, TaskRecord) else None,
                tenant_id=tenant_id,
            )
            gate_id = str(gate_row.id)

        return {
            "task": "application.submit",
            "application_id": str(application_uuid),
            "gate": {
                "should_refresh": bool(verdict.should_refresh) if verdict else None,
                "reason": verdict.reason if verdict else None,
                "age_hours": verdict.age_hours if verdict else None,
                "budget_hours": verdict.budget_hours if verdict else None,
            }
            if verdict is not None
            else None,
            "gate_request_id": gate_id,
            "status": "waiting_human" if gate_id else "not_implemented",
            "detail": (
                "Pre-submit gate parked the task at waiting_human; "
                "approval/rejection enqueues a follow-up."
            )
            if gate_id
            else (
                "Pre-submit gate cleared; click-submit is owned by "
                "src.execution and not invoked from this worker body."
            ),
        }


# ---- Tasks: maintenance ----------------------------------------------


@celery_app.task(name="maintenance.status_sync", base=AutoApplyTask, bind=True)
def status_sync(self: AutoApplyTask, **payload: Any) -> dict[str, Any]:
    """Phase 18.1: outcome-sync is not yet wired. We accept the
    payload (so audit-rows stay consistent) and return
    ``not_implemented`` instead of pretending. The Beat schedule no
    longer references this task; the entry survives so manual CLI
    invocations don't get a registration error."""
    args = _coerce(StatusSyncPayload, payload)
    logger.info("status_sync application_id=%s", args.application_id)
    return {
        "task": "maintenance.status_sync",
        "application_id": args.application_id,
        "status": "not_implemented",
        "detail": (
            "Application-outcome sync should start with supported ATS / "
            "application-portal polling, then add HR-reply / rejection-email "
            "ingestion. Task body is intentionally a no-op so manual "
            "invocations audit honestly."
        ),
    }


@celery_app.task(name="maintenance.jd_health_check", base=AutoApplyTask, bind=True)
def jd_health_check(self: AutoApplyTask) -> dict[str, Any]:
    """Phase 18.1: drive the Phase 13.3 freshness state machine's
    time decay.

    Walks every non-terminal ``JobPosting``, applies
    :func:`project_by_time`, and writes the projected state back if
    it differs. The query is bounded -- ``new`` / ``expired`` /
    ``archived`` don't decay further, and ``last_checked_at IS NULL``
    rows are skipped.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import JobPosting  # noqa: PLC0415
    from src.jobs.state import project_by_time  # noqa: PLC0415
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"
    now = datetime.now(UTC)
    transitions: dict[str, int] = {}
    examined = 0

    factory = get_session_factory()
    with factory() as session, session.begin():
        stmt = (
            select(JobPosting)
            .where(JobPosting.tenant_id == tenant_id)
            .where(JobPosting.state.in_(("active", "stale", "unknown")))
        )
        for posting in session.execute(stmt).scalars():
            examined += 1
            verdict = project_by_time(
                posting.state,
                last_checked_at=posting.last_checked_at,
                now=now,
            )
            if verdict.state != posting.state:
                transitions[f"{posting.state}->{verdict.state}"] = (
                    transitions.get(f"{posting.state}->{verdict.state}", 0) + 1
                )
                posting.state = verdict.state

    return {
        "task": "maintenance.jd_health_check",
        "examined": examined,
        "transitions": transitions,
        "status": "ok",
    }


@celery_app.task(name="maintenance.linkedin_cookie_refresh", base=AutoApplyTask, bind=True)
def linkedin_cookie_refresh(self: AutoApplyTask) -> dict[str, Any]:
    """Phase 18.1: probe the LinkedIn session and record pass/fail.

    The probe is forced (bypasses the in-process cache) so the daily
    Beat tick gives the operator an up-to-date signal. We don't try
    to re-login here -- that requires user interaction. The session
    object's own cache is updated by the probe so subsequent web-UI
    mounts read the same fresh result.
    """
    import asyncio  # noqa: PLC0415

    from src.intake.linkedin import get_linkedin_session_status  # noqa: PLC0415

    try:
        result = asyncio.run(get_linkedin_session_status(force_refresh=True))
    except Exception as exc:  # noqa: BLE001
        logger.exception("linkedin_cookie_refresh probe failed")
        return {
            "task": "maintenance.linkedin_cookie_refresh",
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "task": "maintenance.linkedin_cookie_refresh",
        "authenticated": result.get("authenticated"),
        "has_session_data": result.get("has_session_data"),
        "message": result.get("message"),
        "checked_at": result.get("checked_at"),
        "status": "ok" if result.get("ok") else "error",
    }


@celery_app.task(name="maintenance.cache_eviction", base=AutoApplyTask, bind=True)
def cache_eviction(self: AutoApplyTask) -> dict[str, Any]:
    """Phase 18.4: drive the artifact cleanup + quarantine pipeline.

    The Beat schedule fires this hourly on the :30 minute. Each tick:

    1. Walks ``data/output`` and moves eligible orphan / tmp / failed
       artifacts to ``data/quarantine/<run_id>/`` (writes a
       :class:`CleanupRun` + per-file :class:`CleanupItem` audit).
    2. Then purges any quarantine entries older than
       ``cleanup.quarantine_days`` so the recovered-bytes accounting
       stays accurate.

    The name (``cache_eviction``) is preserved from Phase 14 for Beat
    schedule continuity; the body is now the real cleanup, not a stub.
    DB / FS errors are caught + recorded into the task return value so
    the audit row can still surface them without bouncing the worker.
    """
    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.maintenance.artifacts import (  # noqa: PLC0415
        clean,
        purge_quarantine,
    )
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"
    factory = get_session_factory()

    summaries: dict[str, dict[str, Any]] = {}
    try:
        with factory() as session, session.begin():
            clean_report = clean(
                session, tenant_id=tenant_id, trigger="scheduled"
            )
            summaries["clean"] = clean_report.to_summary()
        with factory() as session, session.begin():
            purge_report = purge_quarantine(
                session, tenant_id=tenant_id, trigger="scheduled"
            )
            summaries["purge_quarantine"] = purge_report.to_summary()
    except Exception as exc:  # noqa: BLE001 -- never crash the worker
        logger.exception("cache_eviction: cleanup pipeline failed")
        summaries["error"] = f"{type(exc).__name__}: {exc}"

    return {
        "task": "maintenance.cache_eviction",
        "status": "ok" if "error" not in summaries else "error",
        "summaries": summaries,
    }


@celery_app.task(name="maintenance.gate_expire_sweep", base=AutoApplyTask, bind=True)
def gate_expire_sweep(self: AutoApplyTask) -> dict[str, Any]:
    """Phase 18.1: flip ``gate_queue`` rows past their TTL to
    ``expired``.

    Walks pending rows whose ``ttl_seconds`` is set and whose
    ``requested_at + ttl_seconds`` is in the past. Each transition
    routes through :func:`src.tasks.gate.expire` so the linked
    ``TaskRecord.last_error`` stays accurate.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import GateRequest  # noqa: PLC0415
    from src.tasks import gate  # noqa: PLC0415
    from src.tasks.context import current_tenant_id  # noqa: PLC0415

    tenant_id = current_tenant_id() or "default"
    now = datetime.now(UTC)
    expired_ids: list[str] = []

    factory = get_session_factory()
    with factory() as session, session.begin():
        stmt = (
            select(GateRequest)
            .where(GateRequest.tenant_id == tenant_id)
            .where(GateRequest.status == gate.STATUS_PENDING)
            .where(GateRequest.ttl_seconds.is_not(None))
        )
        for row in session.execute(stmt).scalars():
            deadline = row.requested_at + timedelta(seconds=int(row.ttl_seconds or 0))
            if deadline <= now:
                gate.expire(session, row.id)
                expired_ids.append(str(row.id))

    return {
        "task": "maintenance.gate_expire_sweep",
        "expired": len(expired_ids),
        "expired_ids": expired_ids,
        "status": "ok",
    }


# ---- Helper: discovery list for CLI introspection -------------------

#: Names workers know how to handle. ``autoapply tasks list`` reads this.
KNOWN_TASK_NAMES: tuple[str, ...] = (
    "search.refresh",
    "search.daily_fanout",
    "jobs.enrich",
    "posting.tag",
    "posting.tag_backfill",
    "materials.generate",
    "application.prepare",
    "application.fill",
    "application.submit",
    "orchestration.plan_run",
    "notifications.morning_digest",
    "maintenance.status_sync",
    "maintenance.jd_health_check",
    "maintenance.linkedin_cookie_refresh",
    "maintenance.cache_eviction",
    "maintenance.gate_expire_sweep",
)


__all__ = [
    "ApplicationFillPayload",
    "ApplicationPreparePayload",
    "ApplicationSubmitPayload",
    "JobEnrichPayload",
    "KNOWN_TASK_NAMES",
    "MaterialsGeneratePayload",
    "OrchestrationPlanRunPayload",
    "PostingTagBackfillPayload",
    "PostingTagPayload",
    "SearchRefreshPayload",
    "StatusSyncPayload",
    "application_fill",
    "application_prepare",
    "application_submit",
    "cache_eviction",
    "gate_expire_sweep",
    "jd_health_check",
    "jobs_enrich",
    "linkedin_cookie_refresh",
    "materials_generate",
    "orchestration_plan_run",
    "posting_tag",
    "posting_tag_backfill",
    "search_daily_fanout",
    "search_refresh",
    "status_sync",
]
