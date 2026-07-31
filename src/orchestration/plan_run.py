"""Phase 17.1: ``plan_run`` orchestrator.

One invocation per scheduled Plan tick — could be hourly, daily, weekly,
or a manual "Run now". The name no longer implies a specific time of day.

* **Search** -- ``application.jobs.search_jobs`` with
  ``use_job_index=True``, which routes through Phase 13.4
  ``cached_search`` (cache-first, refresh stale via
  ``jobs.freshness.should_refresh(context="generate_materials")``).
* **Filter** -- ``matching.scorer.score_jobs`` with the active
  applicant profile; each ``ScoreBreakdown`` carries the Phase 16.1
  structured ``disqualify_results`` for the review-queue UI.
* **Top-N selection** -- qualified jobs ranked by ``final_score``,
  capped at ``top_n``.
* **Enqueue** -- per top-N job: create one canonical ``Application`` and
  ReviewQueueEntry, then enqueue ``materials.generate`` and
  ``application.prepare`` with separate posting/application ids. Prepare waits
  for materials and idempotently fans out ``application.fill``.

Boundaries
----------
* **Never auto-submits.** The orchestrator stops after fill reaches
  ``REVIEW_REQUIRED``. ``application.submit`` lands on the worker only after a
  human approval (manually or via the explicit ``after_approval`` policy) in the
  review queue, and even then the Phase 17.5 pre-submit hard gate
  re-runs ``should_refresh(..., "before_submit")``.
* **Per-tenant.** The Phase 14 ``tenant_id`` ContextVar must be set
  before ``run_plan`` is invoked (the Celery task wrapper handles
  this via ``AutoApplyTask.before_start``; CLI/test callers pass the
  tenant explicitly).
* **Pause-aware.** When the kill-switch sentinel
  (``data/plan_runs_paused``) exists, ``run_plan`` short-circuits
  with ``status="paused"`` so a scheduled tick doesn't generate cost
  on vacation.
* **Idempotent dry-run.** ``dry_run=True`` runs the search + filter
  but skips enqueue. Useful for the Phase 17.6 morning digest
  rehearsal and for CI.

Returned :class:`PlanRunReport` is JSON-serializable so the Phase 14
audit row can store it verbatim and the Phase 17.6 digest can read it
back without ORM access.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Where the Phase 17.7 kill switch lives. A tenant-aware version would
# nest by tenant; we keep one global sentinel for now since multi-
# tenancy hardening is Phase 18.
PLAN_RUN_PAUSE_SENTINEL_NAME = "plan_runs_paused"


@dataclass
class PlanRunReport:
    """Per-invocation summary persisted to the Phase 14 task audit row.

    All fields are intentionally JSON-serializable scalars / lists so
    the Phase 17.6 morning digest can read this back without a
    SQLAlchemy session.
    """

    run_id: str
    tenant_id: str
    profile_id: str
    search_profile_id: str | None
    status: str  # "ok" | "paused" | "no_profile" | "no_results" | "error"
    started_at: str  # ISO 8601 UTC
    finished_at: str
    duration_seconds: float
    top_n: int
    total_jobs_seen: int = 0
    qualified: int = 0
    disqualified: int = 0
    borderline: int = 0  # count of jobs whose final_score sits in [0.4, 0.6]
    selected: int = 0  # jobs that actually reached the enqueue step
    materials_task_ids: list[str] = field(default_factory=list)
    application_prepare_task_ids: list[str] = field(default_factory=list)
    application_submit_task_ids: list[str] = field(default_factory=list)
    application_ids: list[str] = field(default_factory=list)
    review_entry_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    estimated_cost_usd: float = 0.0  # Phase 17.6 fills this with real telemetry
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Type aliases for the dependency-injected callables. Real callers wire
# these to ``application.jobs.search_jobs`` /
# ``matching.scorer.score_jobs`` / Celery's ``send_task``; tests inject
# stubs that don't touch Redis or the network.
@dataclass(frozen=True)
class ApplicationBinding:
    """Canonical ids created before task fan-out for one selected posting."""

    job_posting_id: str
    application_id: str
    review_entry_id: str


SearchFn = Callable[..., Awaitable[dict[str, Any]]]
ScoreFn = Callable[[list[Any], Any], list[Any]]
EnqueueFn = Callable[[str, dict[str, Any]], str]
PrepareApplicationsFn = Callable[..., list[ApplicationBinding]]


class PlanRunError(Exception):
    """Raised on programmer error (missing tenant, etc.). Worker
    failures are captured into :class:`PlanRunReport.errors` instead --
    a partial run should still produce a report so the digest has
    something to show."""


def plan_run_pause_sentinel_path(root: Path | None = None) -> Path:
    """The well-known sentinel path the kill switch (17.7) creates.

    Centralised here so the orchestrator + CLI + tests agree on it.
    """
    from src.core.config import PROJECT_ROOT  # local import; avoid cycle

    base = root if root is not None else PROJECT_ROOT
    return base / "data" / PLAN_RUN_PAUSE_SENTINEL_NAME


def plan_runs_paused(root: Path | None = None) -> bool:
    """Return True iff the sentinel exists.

    A symlink with no target counts as paused; that lets ops scripts
    park the sentinel however they like (touch / ln -s / mv).
    """
    return plan_run_pause_sentinel_path(root).exists()


def _now_utc() -> datetime:
    """Wall-clock now in UTC. Stubbed in tests via monkeypatch."""
    return datetime.now(UTC)


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()


def _borderline_count(breakdowns: list[Any]) -> int:
    """Count qualified breakdowns whose final_score sits in [0.4, 0.6]
    (matches :data:`src.matching.edge_case_agent.BORDERLINE_LOW`).

    Imported lazily to keep this module light when the matching
    package isn't loaded (e.g. unit tests of the report shape).
    """
    from src.matching.edge_case_agent import BORDERLINE_HIGH, BORDERLINE_LOW

    return sum(
        1
        for b in breakdowns
        if not getattr(b, "disqualified", False)
        and BORDERLINE_LOW <= getattr(b, "final_score", 0.0) <= BORDERLINE_HIGH
    )


def normalize_submit_policy(*, submit_policy: str | None, auto_submit: bool) -> str:
    """Resolve the explicit policy and the deprecated boolean alias.

    ``auto_submit=True`` never means "submit immediately". During the
    compatibility window it maps to ``after_approval`` so a human decision
    and the pre-submit freshness gate remain mandatory.
    """
    if submit_policy is None:
        return "after_approval" if auto_submit else "manual"
    normalized = str(submit_policy).strip().lower()
    if normalized not in {"manual", "after_approval"}:
        raise PlanRunError(
            "submit_policy must be 'manual' or 'after_approval'"
        )
    return normalized


async def run_plan(
    *,
    tenant_id: str,
    profile_id: str = "default",
    search_profile_id: str | None = None,
    top_n: int = 10,
    dry_run: bool = False,
    auto_submit: bool = False,
    submit_policy: str | None = None,
    skip_previously_applied: bool = True,
    scrape_enabled: bool = True,
    # Phase 17.8 / 18.x: optional per-plan material strategy overrides.
    # ``None`` for any of these means "let the materials.generate task
    # fall back to the user's Settings → Default material strategy".
    resume_strategy: str | None = None,
    resume_template_id: str | None = None,
    resume_source_document_id: str | None = None,
    resume_patch_aggressiveness: str | None = None,
    resume_patch_allow_reorder_sections: bool | None = None,
    resume_patch_allow_add_remove_bullets: bool | None = None,
    cover_letter_strategy: str | None = None,
    cover_letter_template_id: str | None = None,
    cover_letter_source_document_id: str | None = None,
    cover_letter_patch_aggressiveness: str | None = None,
    cover_letter_patch_allow_reorder_sections: bool | None = None,
    cover_letter_patch_allow_add_remove_bullets: bool | None = None,
    search_fn: SearchFn | None = None,
    score_fn: ScoreFn | None = None,
    enqueue_fn: EnqueueFn | None = None,
    prepare_applications_fn: PrepareApplicationsFn | None = None,
    pause_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> PlanRunReport:
    """Execute one plan run and return a structured report.

    Args:
        tenant_id: Required. Phase 14 tenant context.
        profile_id: Applicant profile to score against. ``"default"``
            uses the YAML pointed at by ``active_profile.txt``.
        search_profile_id: Saved web-search profile. ``None`` falls
            back to the applicant ``profile_id`` (the existing
            ``search_jobs`` convention).
        top_n: Cap on jobs that reach the enqueue step. The deterministic
            scorer's `final_score` ranks the qualified pool.
        dry_run: Run search + filter but skip enqueue. ``status`` stays
            ``"ok"``; ``materials_task_ids`` / ``application_prepare_task_ids``
            stay empty.
        search_fn: Override for the search use case. Real callers leave
            this ``None`` so the real ``search_jobs`` runs.
        score_fn: Override for the scoring pipeline. Real callers leave
            this ``None``.
        enqueue_fn: Function that takes ``(task_name, payload)`` and
            returns the task id. Real callers pass a Celery
            ``send_task`` wrapper; tests pass a list-appender.
        pause_root: Override for the kill-switch sentinel root (Phase
            17.7). ``None`` uses ``PROJECT_ROOT``.
        now: Clock injection for tests.

    Returns:
        :class:`PlanRunReport` -- never raises for runtime failures
        (those are folded into ``errors``); raises
        :class:`PlanRunError` only on programmer errors.
    """
    if not tenant_id:
        raise PlanRunError("tenant_id is required")
    effective_submit_policy = normalize_submit_policy(
        submit_policy=submit_policy,
        auto_submit=auto_submit,
    )

    now_fn = now or _now_utc
    started_at = now_fn()
    run_id = str(uuid.uuid4())
    errors: list[str] = []

    # ----- 0. Kill switch (17.7) ------------------------------------
    if plan_runs_paused(pause_root):
        finished_at = now_fn()
        logger.info("plan_run paused via sentinel; run_id=%s", run_id)
        return PlanRunReport(
            run_id=run_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
            search_profile_id=search_profile_id,
            status="paused",
            started_at=_isoformat(started_at),
            finished_at=_isoformat(finished_at),
            duration_seconds=(finished_at - started_at).total_seconds(),
            top_n=top_n,
            dry_run=dry_run,
        )

    if score_fn is None:
        from src.application.profile import get_profile_path  # noqa: PLC0415

        profile_path = get_profile_path(profile_id)
        if not profile_path.exists():
            finished_at = now_fn()
            error = f"profile {profile_id!r} not found at {profile_path}"
            logger.info("plan_run skipped: %s; run_id=%s", error, run_id)
            return PlanRunReport(
                run_id=run_id,
                tenant_id=tenant_id,
                profile_id=profile_id,
                search_profile_id=search_profile_id,
                status="no_profile",
                started_at=_isoformat(started_at),
                finished_at=_isoformat(finished_at),
                duration_seconds=(finished_at - started_at).total_seconds(),
                top_n=top_n,
                errors=[error],
                dry_run=dry_run,
            )

    # ----- 1. Search ------------------------------------------------
    del scrape_enabled  # Search currently always refreshes through search_jobs.
    search_fn = search_fn or _default_search_fn
    try:
        search_result = await search_fn(
            profile=search_profile_id or profile_id,
            source="all",
            score=False,  # we run scoring ourselves below so we can
                          # capture the structured breakdowns
            use_job_index=True,
            include_views=True,
        )
    except Exception as exc:  # noqa: BLE001 -- worker must keep going
        logger.exception("plan_run search failed; run_id=%s", run_id)
        finished_at = now_fn()
        errors.append(f"search: {type(exc).__name__}: {exc}")
        return PlanRunReport(
            run_id=run_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
            search_profile_id=search_profile_id,
            status="error",
            started_at=_isoformat(started_at),
            finished_at=_isoformat(finished_at),
            duration_seconds=(finished_at - started_at).total_seconds(),
            top_n=top_n,
            errors=errors,
            dry_run=dry_run,
        )

    jobs = list(search_result.get("jobs") or search_result.get("items") or [])
    total_jobs_seen = len(jobs)

    # No results is a *legitimate* outcome (LinkedIn returned nothing
    # for the profile during this run). Still produce a report so the
    # digest reads "0 new jobs" rather than "missing".
    if not jobs:
        finished_at = now_fn()
        logger.info("plan_run found no jobs; run_id=%s", run_id)
        return PlanRunReport(
            run_id=run_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
            search_profile_id=search_profile_id,
            status="no_results",
            started_at=_isoformat(started_at),
            finished_at=_isoformat(finished_at),
            duration_seconds=(finished_at - started_at).total_seconds(),
            top_n=top_n,
            total_jobs_seen=0,
            dry_run=dry_run,
        )

    # ----- 2. Score + filter ---------------------------------------
    # Production wiring needs ``tenant_id`` so it can resolve
    # ``RawJob.id`` -> ``JobPosting.id`` for the review queue rows
    # (codex P1 fix). The 2-arg ``ScoreFn`` contract stays unchanged
    # so existing test stubs are untouched.
    if score_fn is None:
        score_fn = functools.partial(_default_score_fn, tenant_id=tenant_id)
    try:
        breakdowns = score_fn(jobs, profile_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("plan_run scoring failed; run_id=%s", run_id)
        finished_at = now_fn()
        errors.append(f"score: {type(exc).__name__}: {exc}")
        return PlanRunReport(
            run_id=run_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
            search_profile_id=search_profile_id,
            status="error",
            started_at=_isoformat(started_at),
            finished_at=_isoformat(finished_at),
            duration_seconds=(finished_at - started_at).total_seconds(),
            top_n=top_n,
            total_jobs_seen=total_jobs_seen,
            errors=errors,
            dry_run=dry_run,
        )

    qualified = [b for b in breakdowns if not getattr(b, "disqualified", False)]
    disqualified = total_jobs_seen - len(qualified)
    borderline = _borderline_count(breakdowns)

    # Top-N already sorted descending by score_jobs. ``top_n <= 0`` is
    # an explicit "select none" -- codex P2 fix; the previous expression
    # treated 0/negative as "no cap" which silently fanned out tasks
    # for the entire qualified pool when the operator intended to
    # enqueue nothing.
    selected = qualified[:top_n] if top_n > 0 else []
    if skip_previously_applied:
        selected = _drop_previously_applied(tenant_id=tenant_id, selected=selected)

    # ----- 3. Create canonical applications, review rows, then enqueue.
    materials_ids: list[str] = []
    application_prepare_ids: list[str] = []
    # Submission is intentionally not enqueued by the plan. The policy is
    # persisted on Application and evaluated only after human approval.
    application_submit_ids: list[str] = []
    application_ids: list[str] = []
    review_entry_ids: list[str] = []

    if not dry_run:
        enqueue_fn = enqueue_fn or _default_enqueue_fn
        prepare_applications_fn = (
            prepare_applications_fn or _create_applications_and_review_entries
        )
        bindings: list[ApplicationBinding] = []
        try:
            bindings = prepare_applications_fn(
                tenant_id=tenant_id,
                run_id=run_id,
                selected=selected,
                profile_id=profile_id,
                submit_policy=effective_submit_policy,
            )
            application_ids = [binding.application_id for binding in bindings]
            review_entry_ids = [binding.review_entry_id for binding in bindings]
        except Exception as exc:  # noqa: BLE001 -- non-fatal; record + continue
            logger.exception("plan_run: application persistence failed")
            errors.append(f"applications: {type(exc).__name__}: {exc}")

        bindings_by_posting = {
            binding.job_posting_id: binding for binding in bindings
        }
        for breakdown in selected:
            job_id = str(getattr(breakdown, "job_id", "") or "")
            binding = bindings_by_posting.get(job_id)
            if binding is None:
                errors.append(
                    f"no Application binding for job_posting_id={job_id}; "
                    "skipping enqueue"
                )
                continue
            try:
                materials_payload: dict[str, Any] = {
                    "job_id": job_id,
                    "application_id": binding.application_id,
                    "profile_id": profile_id,
                    "document_types": ["resume", "cover_letter"],
                }
                # Only include override keys when the plan actually provided
                # them, preserving Settings defaults for omitted values.
                if resume_strategy:
                    materials_payload["resume_strategy"] = resume_strategy
                if resume_template_id:
                    materials_payload["resume_template_id"] = resume_template_id
                if resume_source_document_id:
                    materials_payload["resume_source_document_id"] = resume_source_document_id
                if resume_patch_aggressiveness:
                    materials_payload["resume_patch_aggressiveness"] = (
                        resume_patch_aggressiveness
                    )
                if resume_patch_allow_reorder_sections is not None:
                    materials_payload["resume_patch_allow_reorder_sections"] = (
                        resume_patch_allow_reorder_sections
                    )
                if resume_patch_allow_add_remove_bullets is not None:
                    materials_payload["resume_patch_allow_add_remove_bullets"] = (
                        resume_patch_allow_add_remove_bullets
                    )
                if cover_letter_strategy:
                    materials_payload["cover_letter_strategy"] = cover_letter_strategy
                if cover_letter_template_id:
                    materials_payload["cover_letter_template_id"] = cover_letter_template_id
                if cover_letter_source_document_id:
                    materials_payload["cover_letter_source_document_id"] = (
                        cover_letter_source_document_id
                    )
                if cover_letter_patch_aggressiveness:
                    materials_payload["cover_letter_patch_aggressiveness"] = (
                        cover_letter_patch_aggressiveness
                    )
                if cover_letter_patch_allow_reorder_sections is not None:
                    materials_payload["cover_letter_patch_allow_reorder_sections"] = (
                        cover_letter_patch_allow_reorder_sections
                    )
                if cover_letter_patch_allow_add_remove_bullets is not None:
                    materials_payload["cover_letter_patch_allow_add_remove_bullets"] = (
                        cover_letter_patch_allow_add_remove_bullets
                    )

                mat_id = enqueue_fn("materials.generate", materials_payload)
                materials_ids.append(mat_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"materials.generate enqueue: {type(exc).__name__}: {exc}"
                )
                continue

            try:
                prep_id = enqueue_fn(
                    "application.prepare",
                    {"application_id": binding.application_id},
                )
                application_prepare_ids.append(prep_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"application.prepare enqueue: {type(exc).__name__}: {exc}"
                )
    finished_at = now_fn()
    status = "ok" if not errors else "error"
    return PlanRunReport(
        run_id=run_id,
        tenant_id=tenant_id,
        profile_id=profile_id,
        search_profile_id=search_profile_id,
        status=status,
        started_at=_isoformat(started_at),
        finished_at=_isoformat(finished_at),
        duration_seconds=(finished_at - started_at).total_seconds(),
        top_n=top_n,
        total_jobs_seen=total_jobs_seen,
        qualified=len(qualified),
        disqualified=disqualified,
        borderline=borderline,
        selected=len(selected),
        materials_task_ids=materials_ids,
        application_prepare_task_ids=application_prepare_ids,
        application_submit_task_ids=application_submit_ids,
        application_ids=application_ids,
        review_entry_ids=review_entry_ids,
        errors=errors,
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------- #
# Default dependency wiring                                                   #
# --------------------------------------------------------------------------- #
# These are the production wires. ``run_plan`` accepts overrides so the
# Celery task wrapper, the CLI, and the test suite can each substitute
# what they need without re-importing the orchestrator.


async def _default_search_fn(**kwargs: Any) -> dict[str, Any]:
    """Lazy import to keep this module light when scoring tests import it."""
    from src.application.jobs import search_jobs

    return await search_jobs(**kwargs)


def _coerce_job_to_rawjob(job: Any) -> Any | None:
    """Convert ``application.jobs.serialize_job`` output back to ``RawJob``.

    Codex P1 fix: ``application.jobs.search_jobs`` returns a list of
    serialized dicts (the same shape the SPA consumes), but
    ``matching.scorer.score_jobs`` expects ``RawJob`` Pydantic objects.
    Without this conversion every real plan run would crash inside
    scoring and the report would land with ``status="error"``.

    Items that are already ``RawJob`` pass through (so test stubs that
    inject raw objects keep working). Items that can't be coerced are
    dropped with a logged warning so one malformed row doesn't blank
    the whole run.
    """
    from src.application.matching import _coerce_to_raw_job  # noqa: PLC0415
    from src.intake.schema import RawJob  # noqa: PLC0415

    if isinstance(job, RawJob):
        return job
    if isinstance(job, dict):
        coerced = _coerce_to_raw_job(job)
        if coerced is None:
            logger.warning(
                "plan_run: dropping unscoreable job: id=%s company=%s",
                job.get("id"),
                job.get("company"),
            )
        return coerced
    # Some other shape (e.g. a Pydantic model from a future scraper).
    # Use it as-is and let scoring complain if it doesn't match.
    return job


def _default_score_fn(
    jobs: list[Any], profile_id: str, *, tenant_id: str = ""
) -> list[Any]:
    """Default wiring: load YAML, build scoring context, score the batch.

    Coerces serialized-dict jobs back to :class:`RawJob` first (see
    :func:`_coerce_job_to_rawjob`) so the production search path is
    actually scoreable.

    Then (codex P1 fix) resolves the persistent ``JobPosting.id`` +
    ``latest_snapshot_id`` for each scored job and overwrites
    ``breakdown.job_id`` / ``breakdown.job_snapshot_id`` so the
    pre-submit gate (which queries ``JobPosting`` by id) can find the
    row. Without this, every review entry created by a plan run would
    land with ``RawJob.id`` (a fresh UUID per scrape) and approve+submit
    would always 404 with ``missing_binding``.

    ``tenant_id`` is captured via ``functools.partial`` in
    :func:`run_plan`; the public ``ScoreFn`` contract stays 2-arg so
    test stubs are unaffected.
    """
    from src.application.profile import get_profile_path  # noqa: PLC0415
    from src.matching.scorer import build_scoring_context  # noqa: PLC0415
    from src.matching.scorer import score_jobs as score_ranked  # noqa: PLC0415
    from src.memory.profile import load_profile_yaml  # noqa: PLC0415

    path = get_profile_path(profile_id)
    if path is None or not path.exists():
        raise PlanRunError(f"profile {profile_id!r} not found at {path}")
    profile_data = load_profile_yaml(path)
    ctx = build_scoring_context(profile_data)

    raw_jobs = [_coerce_job_to_rawjob(j) for j in jobs]
    raw_jobs = [j for j in raw_jobs if j is not None]

    # Phase 19.6: consult the A2 score cache up front. For raw_jobs
    # whose snapshot + (profile_version, scorer_version) already has a
    # cached verdict, build a :class:`ScoreBreakdown` from the cached
    # row and skip the deterministic scorer entirely. Cache misses fall
    # through to ``score_ranked`` so the existing path is intact.
    cache_hits: list[Any] = []
    a1_rejects: list[Any] = []
    miss_raw_jobs: list[Any] = raw_jobs
    a1_resolution: dict[str, tuple[Any, Any]] = {}
    if tenant_id and raw_jobs:
        try:
            cache_hits, miss_raw_jobs, a1_resolution = _consume_score_cache(
                raw_jobs=raw_jobs,
                tenant_id=tenant_id,
                profile_id=profile_id,
                profile_data=profile_data,
            )
        except Exception:  # noqa: BLE001 -- cache failure is non-fatal
            logger.exception(
                "plan_run: score cache pre-pass failed; falling back to full rescore"
            )
            cache_hits, miss_raw_jobs, a1_resolution = [], raw_jobs, {}

        try:
            a1_rejects, miss_raw_jobs = _consume_a1_rejects(
                raw_jobs=miss_raw_jobs,
                tenant_id=tenant_id,
                profile_data=profile_data,
                resolution=a1_resolution,
            )
        except Exception:  # noqa: BLE001 -- tag cache failure is non-fatal
            logger.exception(
                "plan_run: A1 fast-path pre-pass failed; falling back to scoring"
            )
            a1_rejects, miss_raw_jobs = [], miss_raw_jobs

    scored_breakdowns = score_ranked(miss_raw_jobs, ctx) if miss_raw_jobs else []
    breakdowns = cache_hits + a1_rejects + scored_breakdowns
    breakdowns.sort(key=lambda b: getattr(b, "final_score", 0.0), reverse=True)

    if tenant_id:
        try:
            _resolve_and_patch_posting_ids(scored_breakdowns, miss_raw_jobs, tenant_id)
        except Exception:  # noqa: BLE001 - non-fatal; logged
            logger.exception(
                "plan_run: posting-id resolution failed; review entries "
                "will land with RawJob.id and pre-submit may fail"
            )

        # Phase 19.6: apply A1 fast-path rejects. The deterministic
        # scorer already disqualifies on hard rules over the raw JD; the
        # A1 layer adds snapshot-tag-based rejects that don't require
        # re-parsing the JD text (e.g. ``sponsorship_signal=not_offered``).
        # Disqualified breakdowns get ``disqualify_reasons`` extended; the
        # cache write-through below still records the verdict for audit.
        try:
            _apply_a1_rejects(
                breakdowns=breakdowns,
                tenant_id=tenant_id,
                profile_data=profile_data,
            )
        except Exception:  # noqa: BLE001 -- never bounce a successful score
            logger.exception("plan_run: A1 fast-path rejects failed")

        # Phase 19.4: write-through to job_posting_scores. Skip the
        # cached-hit breakdowns (they came from the cache row we'd be
        # re-inserting, and on-conflict-do-nothing would no-op anyway).
        try:
            _persist_score_cache(
                breakdowns=scored_breakdowns,
                tenant_id=tenant_id,
                profile_id=profile_id,
                profile_data=profile_data,
            )
        except Exception:  # noqa: BLE001 -- never bounce a successful score
            logger.exception("plan_run: score cache write-through failed")

    _ = a1_resolution  # currently informational; reserved for future A1 telemetry
    return breakdowns


def _consume_score_cache(
    *,
    raw_jobs: list[Any],
    tenant_id: str,
    profile_id: str,
    profile_data: dict[str, Any],
) -> tuple[list[Any], list[Any], dict[str, tuple[Any, Any]]]:
    """Pre-pass that turns the A2 score cache into actual fast-path savings.

    Resolves each ``RawJob.id -> (posting_id, snapshot_id)`` via the
    ``JobPosting`` row, then looks up
    ``(tenant_id, snapshot_id, profile_id, profile_version, scorer_version)``
    in :mod:`src.filter.score_cache`. For each hit we synthesise a
    :class:`ScoreBreakdown` from the cached payload so the downstream
    plan_run flow keeps working unchanged; misses fall through to the
    real scorer.

    Returns ``(cache_hit_breakdowns, miss_raw_jobs, resolution_map)``.
    """
    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import JobPosting  # noqa: PLC0415
    from src.filter.score_cache import (  # noqa: PLC0415
        compute_profile_version,
        get_cached_score,
    )
    from src.matching.scorer import ScoreBreakdown  # noqa: PLC0415

    if not raw_jobs:
        return [], [], {}

    # Build (source, source_id) keys and map RawJob.id -> key.
    rawjob_id_to_key: dict[str, tuple[str, str]] = {}
    keys: set[tuple[str, str]] = set()
    for rj in raw_jobs:
        source = getattr(rj, "source", None)
        source_id = getattr(rj, "source_id", None)
        rj_id = getattr(rj, "id", None)
        if source and source_id and rj_id is not None:
            key = (str(source), str(source_id))
            keys.add(key)
            rawjob_id_to_key[str(rj_id)] = key

    if not keys:
        return [], list(raw_jobs), {}

    from sqlalchemy import and_, or_, select  # noqa: PLC0415

    profile_version = compute_profile_version(profile_data)
    factory = get_session_factory()

    cache_hits: list[Any] = []
    miss_raw_jobs: list[Any] = []
    resolution: dict[str, tuple[Any, Any]] = {}

    with factory() as session:
        rows = (
            session.execute(
                select(
                    JobPosting.id,
                    JobPosting.latest_snapshot_id,
                    JobPosting.source,
                    JobPosting.source_id,
                ).where(
                    JobPosting.tenant_id == tenant_id,
                    or_(
                        *[
                            and_(
                                JobPosting.source == s,
                                JobPosting.source_id == sid,
                            )
                            for s, sid in keys
                        ]
                    ),
                )
            )
            .all()
        )
        key_to_ids = {
            (row.source, row.source_id): (row.id, row.latest_snapshot_id)
            for row in rows
        }

        for rj in raw_jobs:
            rj_id = str(getattr(rj, "id", "") or "")
            key = rawjob_id_to_key.get(rj_id)
            persisted = key_to_ids.get(key) if key else None
            if persisted is not None:
                resolution[rj_id] = persisted
            posting_id, snapshot_id = persisted if persisted else (None, None)
            if snapshot_id is None:
                miss_raw_jobs.append(rj)
                continue

            hit = get_cached_score(
                session,
                tenant_id=tenant_id,
                snapshot_id=snapshot_id,
                profile_id=profile_id,
                profile_version=profile_version,
            )
            if hit is None:
                miss_raw_jobs.append(rj)
                continue

            cache_hits.append(
                _breakdown_from_cached_verdict(
                    raw_job=rj,
                    posting_id=posting_id,
                    snapshot_id=snapshot_id,
                    cached=hit,
                    breakdown_cls=ScoreBreakdown,
                )
            )

    logger.info(
        "plan_run: score cache pre-pass tenant=%s hits=%d misses=%d",
        tenant_id,
        len(cache_hits),
        len(miss_raw_jobs),
    )
    return cache_hits, miss_raw_jobs, resolution


def _breakdown_from_cached_verdict(
    *,
    raw_job: Any,
    posting_id: Any,
    snapshot_id: Any,
    cached: Any,
    breakdown_cls: Any,
) -> Any:
    """Rehydrate a :class:`ScoreBreakdown` from a cached row.

    The cached ``score_breakdown`` JSON carries every field
    ``ScoreBreakdown.to_dict()`` produced at write time. We replay the
    salient ones (``final_score``, ``disqualified``, component scores)
    so downstream code -- top-N selection, review-queue rendering, the
    "cache provenance" badge -- gets a breakdown that quacks like a
    freshly computed one. ``job_id`` is set to the persisted
    ``posting_id`` so the pre-submit gate works the same as it does for
    breakdowns that went through :func:`_resolve_and_patch_posting_ids`.
    """
    bd_payload = dict(cached.score_breakdown or {})
    bd = breakdown_cls(
        job_id=str(posting_id),
        company=bd_payload.get("company") or getattr(raw_job, "company", ""),
        title=bd_payload.get("title") or getattr(raw_job, "title", ""),
        job_snapshot_id=str(snapshot_id),
    )
    bd.final_score = float(cached.final_score) if cached.final_score is not None else 0.0
    bd.disqualified = bool(bd_payload.get("disqualified") or cached.verdict == "reject")
    bd.skill_overlap = float(bd_payload.get("skill_overlap") or 0.0)
    bd.keyword_similarity = float(bd_payload.get("keyword_similarity") or 0.0)
    bd.rule_bonus = float(bd_payload.get("rule_bonus") or 0.0)
    bd.quality_multiplier = float(bd_payload.get("quality_multiplier") or 1.0)
    bd.disqualify_reasons = list(bd_payload.get("disqualify_reasons") or [])
    # Stash cache provenance so ReviewQueueView's "cached · profile vXYZ ·
    # scorer sABC" badge can render. The frontend reads
    # ``score_breakdown.cache.{profile_version, scorer_version, computed_at}``.
    bd_payload["cache"] = {
        "profile_version": cached.profile_version,
        "scorer_version": cached.scorer_version,
        "computed_at": cached.computed_at.isoformat() if cached.computed_at else None,
    }
    # Some downstream consumers serialise the breakdown via to_dict();
    # stash the merged payload under a private attribute so a future
    # ``to_dict`` override can return the cached breakdown verbatim. The
    # current code path that reads ``breakdown.to_dict()`` is the
    # write-through which we skip for cache hits, so the field is
    # informational today.
    bd._cached_payload = bd_payload  # type: ignore[attr-defined]
    return bd


def _consume_a1_rejects(
    *,
    raw_jobs: list[Any],
    tenant_id: str,
    profile_data: dict[str, Any],
    resolution: dict[str, tuple[Any, Any]],
) -> tuple[list[Any], list[Any]]:
    """Skip scorer work for jobs whose ready A1 tags fail a hard rule."""
    rules = _build_hard_rules(profile_data)
    if not raw_jobs or not rules.rules or not resolution:
        return [], list(raw_jobs)

    snapshot_ids = []
    for rj in raw_jobs:
        _posting_id, snapshot_id = resolution.get(
            str(getattr(rj, "id", "") or ""), (None, None)
        )
        if snapshot_id is not None:
            snapshot_ids.append(snapshot_id)
    if not snapshot_ids:
        return [], list(raw_jobs)

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.matching.scorer import ScoreBreakdown  # noqa: PLC0415

    factory = get_session_factory()
    with factory() as session:
        decisions = _a1_rejects_for_snapshots(
            session=session,
            tenant_id=tenant_id,
            snapshot_ids=snapshot_ids,
            rules=rules,
        )

    rejects: list[Any] = []
    misses: list[Any] = []
    for rj in raw_jobs:
        rj_id = str(getattr(rj, "id", "") or "")
        posting_id, snapshot_id = resolution.get(rj_id, (None, None))
        decision = decisions.get(str(snapshot_id)) if snapshot_id is not None else None
        if decision is None or posting_id is None:
            misses.append(rj)
            continue
        bd = ScoreBreakdown(
            job_id=str(posting_id),
            company=getattr(rj, "company", ""),
            title=getattr(rj, "title", ""),
            job_snapshot_id=str(snapshot_id),
        )
        bd.disqualified = True
        bd.final_score = 0.0
        bd.rule_bonus = 0.0
        bd.disqualify_reasons = [_a1_reject_reason(decision)]
        rejects.append(bd)

    logger.info(
        "plan_run: A1 fast-path tenant=%s rejects=%d scoring_candidates=%d",
        tenant_id,
        len(rejects),
        len(misses),
    )
    return rejects, misses


def _apply_a1_rejects(
    *,
    breakdowns: list[Any],
    tenant_id: str,
    profile_data: dict[str, Any],
) -> None:
    """Phase 19.6: walk the breakdowns, look up each snapshot's tags,
    and flip the breakdown to ``disqualified=True`` when an A1 hard
    rule rejects.

    Hard rules are derived from the applicant profile (``sponsorship_required``
    when ``profile.work_authorization`` indicates need for visa support,
    ``no_clearance`` when ``profile.clearance=False``). The fast-path
    module is the single source of truth for which tag values trigger
    which rule; we only build the rule set here.
    """
    rules = _build_hard_rules(profile_data)
    if not rules.rules:
        return

    persistable = [
        b
        for b in breakdowns
        if getattr(b, "job_snapshot_id", None) and not getattr(b, "disqualified", False)
    ]
    if not persistable:
        return

    snapshot_ids = [_coerce_uuid(b.job_snapshot_id) for b in persistable]
    from src.core.database import get_session_factory  # noqa: PLC0415

    factory = get_session_factory()
    with factory() as session:
        decisions = _a1_rejects_for_snapshots(
            session=session,
            tenant_id=tenant_id,
            snapshot_ids=snapshot_ids,
            rules=rules,
        )
        for bd in persistable:
            decision = decisions.get(str(_coerce_uuid(bd.job_snapshot_id)))
            if decision is None:
                continue
            bd.disqualified = True
            bd.final_score = 0.0
            reason = _a1_reject_reason(decision)
            existing = list(getattr(bd, "disqualify_reasons", []) or [])
            if reason not in existing:
                existing.append(reason)
            bd.disqualify_reasons = existing


def _a1_rejects_for_snapshots(
    *,
    session: Any,
    tenant_id: str,
    snapshot_ids: list[Any],
    rules: Any,
) -> dict[str, Any]:
    """Bulk-load ready snapshot tags and return the first failed A1 rule."""
    if not snapshot_ids or not getattr(rules, "rules", None):
        return {}

    from sqlalchemy import select  # noqa: PLC0415

    from src.core.models import JobSnapshot  # noqa: PLC0415
    from src.jobs.tag_service import STATUS_READY  # noqa: PLC0415

    stmt = (
        select(JobSnapshot.id, JobSnapshot.tags, JobSnapshot.tags_status)
        .where(JobSnapshot.tenant_id == tenant_id)
        .where(JobSnapshot.id.in_(snapshot_ids))
    )
    decisions: dict[str, Any] = {}
    for row in session.execute(stmt).all():
        if row.tags_status != STATUS_READY:
            continue
        failed = rules.first_failure(dict(row.tags or {}))
        if failed is not None:
            decisions[str(row.id)] = failed
    return decisions


def _a1_reject_reason(decision: Any) -> str:
    return f"A1:{decision.rule_id}:{decision.description or ''}".strip(":")


def _build_hard_rules(profile_data: dict[str, Any]) -> Any:
    """Derive the active A1 hard-rule set from the applicant profile.

    Conservative defaults: rules are only installed when the profile
    explicitly opts in (e.g. needs sponsorship, cannot get a US
    clearance). Profiles that don't mention these flags get an empty
    rule set so existing behaviour is preserved.
    """
    from src.filter import fast_path  # noqa: PLC0415

    rules: list[fast_path.HardRule] = []
    work_auth = (profile_data or {}).get("work_authorization") or {}
    if isinstance(work_auth, dict):
        needs_sponsorship = bool(
            work_auth.get("needs_sponsorship")
            or work_auth.get("requires_sponsorship")
        )
        if needs_sponsorship:
            rules.append(fast_path.sponsorship_required_rule())

    clearance = (profile_data or {}).get("clearance")
    if clearance is False or (isinstance(clearance, dict) and clearance.get("eligible") is False):
        rules.append(fast_path.clearance_excluded_rule())
    return fast_path.HardRuleSet(rules=rules)


def _persist_score_cache(
    *,
    breakdowns: list[Any],
    tenant_id: str,
    profile_id: str,
    profile_data: dict[str, Any],
) -> None:
    """Phase 19.4 write-through.

    One row per ``(snapshot, profile, profile_version, scorer_version)``;
    breakdowns whose ``job_snapshot_id`` is unset (the snapshot has not
    been enriched yet) are skipped because the cache key would be
    ambiguous. The unique constraint guarantees idempotency under races.
    """
    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.filter.score_cache import (  # noqa: PLC0415
        SCORER_VERSION,
        compute_profile_version,
        record_score,
    )

    persistable = [
        b
        for b in breakdowns
        if getattr(b, "job_snapshot_id", None) and getattr(b, "job_id", None)
    ]
    if not persistable:
        return

    profile_version = compute_profile_version(profile_data)
    factory = get_session_factory()
    with factory() as session, session.begin():
        for breakdown in persistable:
            try:
                record_score(
                    session,
                    tenant_id=tenant_id,
                    posting_id=_coerce_uuid(breakdown.job_id),
                    snapshot_id=_coerce_uuid(breakdown.job_snapshot_id),
                    profile_id=profile_id,
                    profile_version=profile_version,
                    breakdown=breakdown,
                    scorer_version=SCORER_VERSION,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "plan_run: score cache row insert failed for snapshot=%s: %s",
                    getattr(breakdown, "job_snapshot_id", None),
                    exc,
                )


def _coerce_uuid(value: Any) -> Any:
    """Pass UUIDs through; parse strings; otherwise return as-is so the
    SQLAlchemy column raises a clear error rather than silently storing
    junk. Tests inject ``str`` ids; production code is already ``UUID``."""
    from uuid import UUID  # noqa: PLC0415

    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return value
    return value


def _resolve_and_patch_posting_ids(
    breakdowns: list[Any],
    raw_jobs: list[Any],
    tenant_id: str,
) -> None:
    """Look up ``JobPosting`` by ``(tenant_id, source, source_id)`` and
    rewrite each breakdown's ``job_id`` / ``job_snapshot_id`` to the
    persisted ids.

    Operates in-place because :class:`ScoreBreakdown` is a dataclass we
    own. RawJob.id → posting.id mapping is keyed on (source, source_id)
    which is the Phase 13 ``uq_job_postings_tenant_source`` constraint.

    Misses (a posting the scorer scored but the job index never saw)
    leave the breakdown unchanged; the review entry will still be
    inserted but the pre-submit gate will report ``missing_binding``
    until the next refresh fills in the posting row. That's strictly
    better than the current behaviour (silent failure on every entry).
    """
    if not breakdowns or not raw_jobs:
        return

    from sqlalchemy import and_, or_, select  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import JobPosting  # noqa: PLC0415

    # Build (source, source_id) keys from raw_jobs and index them by
    # the RawJob.id so we can map breakdown.job_id (RawJob.id as str) ->
    # source key after the DB lookup.
    rawjob_id_to_key: dict[str, tuple[str, str]] = {}
    keys: set[tuple[str, str]] = set()
    for rj in raw_jobs:
        source = getattr(rj, "source", None)
        source_id = getattr(rj, "source_id", None)
        rj_id = getattr(rj, "id", None)
        if source and source_id and rj_id is not None:
            key = (str(source), str(source_id))
            keys.add(key)
            rawjob_id_to_key[str(rj_id)] = key

    if not keys:
        return

    factory = get_session_factory()
    with factory() as session:
        rows = (
            session.execute(
                select(
                    JobPosting.id,
                    JobPosting.latest_snapshot_id,
                    JobPosting.source,
                    JobPosting.source_id,
                ).where(
                    JobPosting.tenant_id == tenant_id,
                    or_(
                        *[
                            and_(
                                JobPosting.source == s,
                                JobPosting.source_id == sid,
                            )
                            for s, sid in keys
                        ]
                    ),
                )
            )
            .all()
        )
    key_to_ids: dict[tuple[str, str], tuple[Any, Any]] = {
        (row.source, row.source_id): (row.id, row.latest_snapshot_id)
        for row in rows
    }

    for bd in breakdowns:
        rj_id = str(getattr(bd, "job_id", "") or "")
        key = rawjob_id_to_key.get(rj_id)
        if key is None:
            continue
        persisted = key_to_ids.get(key)
        if persisted is None:
            # Scored a job that was never persisted (search bypassed the
            # job index, or the row was retention-purged between scrape
            # and score). Leave the breakdown alone -- the review row
            # will still write but pre-submit will fail informatively.
            continue
        posting_id, snapshot_id = persisted
        # Mutate in place. ScoreBreakdown is a dataclass; both fields
        # are typed as ``str | None`` for job_snapshot_id (Phase 16.1)
        # and ``str`` for job_id.
        bd.job_id = str(posting_id)
        if snapshot_id is not None:
            bd.job_snapshot_id = str(snapshot_id)


def _create_applications_and_review_entries(
    *,
    tenant_id: str,
    run_id: str,
    selected: list[Any],
    profile_id: str,
    submit_policy: str,
) -> list[ApplicationBinding]:
    """Create/reuse canonical Application rows and bind review entries.

    A posting id and an application id are deliberately different domains.
    This is the only plan-run boundary that converts a selected
    ``JobPosting`` into an internal ``Application``; all downstream tasks
    receive the resulting application id.
    """
    import uuid as uuid_mod  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from src.application.review import CreateEntryArgs, create_entry  # noqa: PLC0415
    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import (  # noqa: PLC0415
        Application,
        JobPosting,
        JobSnapshot,
    )

    if not selected:
        return []

    factory = get_session_factory()
    bindings: list[ApplicationBinding] = []
    with factory() as session, session.begin():
        for breakdown in selected:
            posting_value = getattr(breakdown, "job_id", None)
            snapshot_value = getattr(breakdown, "job_snapshot_id", None)
            try:
                posting_id = uuid_mod.UUID(str(posting_value))
                snapshot_id = (
                    uuid_mod.UUID(str(snapshot_value))
                    if snapshot_value
                    else None
                )
                with session.begin_nested():
                    posting = session.get(JobPosting, posting_id)
                    if posting is None or posting.tenant_id != tenant_id:
                        raise ValueError(
                            f"JobPosting {posting_id} not found for tenant {tenant_id}"
                        )
                    if snapshot_id is None:
                        snapshot_id = posting.latest_snapshot_id
                    if snapshot_id is not None:
                        snapshot = session.get(JobSnapshot, snapshot_id)
                        if (
                            snapshot is None
                            or snapshot.tenant_id != tenant_id
                            or snapshot.posting_id != posting_id
                        ):
                            raise ValueError(
                                f"JobSnapshot {snapshot_id} is not bound to {posting_id}"
                            )

                    stmt = (
                        select(Application)
                        .where(
                            Application.tenant_id == tenant_id,
                            Application.job_posting_id == posting_id,
                            Application.status != "FAILED",
                        )
                        .order_by(Application.created_at.desc())
                        .limit(1)
                    )
                    if snapshot_id is not None:
                        stmt = stmt.where(
                            Application.job_snapshot_id == snapshot_id
                        )
                    application = session.execute(stmt).scalar_one_or_none()
                    if application is None:
                        application = Application(
                            tenant_id=tenant_id,
                            profile_id=profile_id,
                            job_id=None,
                            job_posting_id=posting_id,
                            job_snapshot_id=snapshot_id,
                            status="DISCOVERED",
                            match_score=getattr(breakdown, "final_score", None),
                            submit_policy=submit_policy,
                        )
                        session.add(application)
                        session.flush()
                    elif submit_policy == "after_approval":
                        # An explicit opt-in may upgrade an existing manual
                        # draft; never downgrade after_approval implicitly.
                        application.submit_policy = submit_policy

                    try:
                        score_breakdown = (
                            breakdown.to_dict()
                            if hasattr(breakdown, "to_dict")
                            else {}
                        )
                    except Exception:  # noqa: BLE001 -- defensive audit payload
                        score_breakdown = {}
                    entry = create_entry(
                        session,
                        CreateEntryArgs(
                            tenant_id=tenant_id,
                            application_id=application.id,
                            job_id=posting_id,
                            job_snapshot_id=snapshot_id,
                            materials_path=None,
                            score_breakdown=score_breakdown,
                            company=getattr(breakdown, "company", None),
                            title=getattr(breakdown, "title", None),
                            run_id=run_id,
                        ),
                    )
                bindings.append(
                    ApplicationBinding(
                        job_posting_id=str(posting_id),
                        application_id=str(application.id),
                        review_entry_id=str(entry.id),
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "plan_run: application binding failed for job_posting_id=%s: %s",
                    posting_value,
                    exc,
                )
    return bindings

def _drop_previously_applied(*, tenant_id: str, selected: list[Any]) -> list[Any]:
    """Remove jobs that already have an application record for this tenant."""
    if not selected:
        return []

    import uuid as uuid_mod  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.core.models import Application  # noqa: PLC0415

    job_ids: list[uuid_mod.UUID] = []
    by_uuid: dict[uuid_mod.UUID, Any] = {}
    for breakdown in selected:
        try:
            job_uuid = uuid_mod.UUID(str(getattr(breakdown, "job_id", "")))
        except ValueError:
            continue
        job_ids.append(job_uuid)
        by_uuid[job_uuid] = breakdown
    if not job_ids:
        return selected

    factory = get_session_factory()
    with factory() as session:
        existing = set(
            session.execute(
                select(Application.job_posting_id).where(
                    Application.tenant_id == tenant_id,
                    Application.job_posting_id.in_(job_ids),
                    Application.status != "FAILED",
                )
            ).scalars()
        )
    return [bd for job_id, bd in by_uuid.items() if job_id not in existing]


def _default_enqueue_fn(task_name: str, payload: dict[str, Any]) -> str:
    """Default wiring: hand off to the Phase 14 Celery app."""
    from src.tasks.app import celery_app  # noqa: PLC0415

    async_result = celery_app.send_task(task_name, kwargs=payload)
    return str(async_result.id)


__all__ = [
    "PLAN_RUN_PAUSE_SENTINEL_NAME",
    "ApplicationBinding",
    "PlanRunError",
    "PlanRunReport",
    "plan_run_pause_sentinel_path",
    "plan_runs_paused",
    "run_plan",
]
