"""Phase 13.4 search flow, refactored in Phase 19.5 to always hit upstream.

The function :func:`cached_search` is the only thing intake callers (the
LinkedIn search wrapper, future ATS-wide search) should reach for. The
Phase 19 contract is:

  1. Normalize the incoming params and upsert the matching ``SearchQuery``
     by (tenant_id, source, normalized_key).
  2. Acquire a Phase 12 distributed lock on the fingerprint so two
     concurrent submissions of the same search don't double-fetch.
  3. Run the user-supplied ``fetch_fn``, persist the returned postings as
     ``search_results`` rows, prune links not seen this run, and stamp
     ``last_run_at`` / ``status``.
  4. On scrape failure the *old* cached results are preserved -- the
     query's ``last_error`` is set and ``status`` flips to ``stale`` so
     the next read knows the cache is degraded, but
     ``cached_search(...).postings`` still returns the previous run's
     rows so the UI doesn't go blank during a LinkedIn auth bounce.

The freshness short-circuit that Phase 13 used is gone (D029): a
fresh TTL hit on the whole result-set was hiding newly posted jobs
between the previous scrape and TTL expiry. The per-posting analysis
caches (snapshot tags in :mod:`src.jobs.tagger`; profile-scoped score
cache in :mod:`src.filter.score_cache`) carry the cost-cutting role
the TTL used to.

``force_refresh`` is now a no-op; it is accepted for backwards
compatibility with callers wired up in Phase 13. The function is
intentionally framework-agnostic. ``fetch_fn`` can be sync or async;
the wrapper does not know about Playwright or httpx, and unit tests
stub it with a list literal.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from src.cache import Cache
from src.jobs.normalize import normalize_search_key, search_query_fingerprint
from src.jobs.store import JobIndexStore

logger = logging.getLogger("autoapply.jobs.search")

# How long a cache-first hit is considered "fresh" by default. Per-context
# overrides live in Phase 13.6; this value is the floor used when the
# query row alone is consulted.
DEFAULT_FRESHNESS_HOURS = 24

# Default lock TTL around a scrape. Larger than the typical LinkedIn
# search budget so a slow page doesn't get the lock yanked while the
# scrape is still in flight; smaller than the scheduler's task lease.
DEFAULT_LOCK_TTL_S = 600


class ScrapedPosting(Protocol):
    """Shape of an item ``fetch_fn`` is expected to return.

    Both the dataclass below and the existing ``intake.schema.RawJob``
    satisfy this protocol by attribute access.
    """

    source: str
    source_id: str
    company: str
    application_url: str | None


@dataclass
class _SimplePosting:
    source: str
    source_id: str
    company: str
    application_url: str | None = None


FetchFn = Callable[[], Iterable[ScrapedPosting] | Awaitable[Iterable[ScrapedPosting]]]


@dataclass
class SearchOutcome:
    """Result of a :func:`cached_search` call.

    ``postings`` is the list of ``JobPosting`` rows (cache hit or fresh).
    ``cached`` is True iff no network call happened. ``stale`` is True
    iff the most recent scrape failed and we're falling back to the
    previous run's rows. ``query_id`` is the persisted ``SearchQuery``
    UUID so the caller can refresh / drill in later.
    """

    postings: list[Any]
    cached: bool
    stale: bool
    query_id: Any
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None = None
    refresh_failed: bool = False
    counts: dict[str, int] = field(default_factory=dict)


async def cached_search(
    *,
    store: JobIndexStore,
    cache: Cache | None,
    source: str,
    params: dict[str, Any],
    fetch_fn: FetchFn,
    max_pages: int | None = None,
    force_refresh: bool = False,
    freshness_hours: int = DEFAULT_FRESHNESS_HOURS,
    lock_ttl: int = DEFAULT_LOCK_TTL_S,
    now: datetime | None = None,
) -> SearchOutcome:
    """Always-refresh search (Phase 19.5). See module docstring."""
    now = now or datetime.now(UTC)
    # ``force_refresh`` / ``freshness_hours`` are kept on the signature
    # for backwards compatibility with Phase 13 callers; the TTL short-
    # circuit they once drove was removed in Phase 19.5 (D029).
    del force_refresh, freshness_hours
    normalized = normalize_search_key(params, source=source)
    fingerprint = search_query_fingerprint(params, source=source)

    lock_key = f"jobs:search:{source}:{fingerprint}"
    cache_lock = cache.lock(lock_key, ttl=lock_ttl) if cache is not None else _NullLock()
    with cache_lock as handle:
        if cache is not None and not handle.acquired:
            # Somebody else is scraping the same query. Return whatever's
            # already cached and surface ``stale=True`` so the UI shows
            # a "refresh in progress" spinner.
            logger.info(
                "Job index lock contention: returning previous results for %s", fingerprint[:12]
            )
            query = store.find_query(source, fingerprint)
            postings = store.get_results(query.id) if query is not None else []
            return SearchOutcome(
                postings=postings,
                cached=True,
                stale=True,
                query_id=query.id if query is not None else None,
                last_run_at=query.last_run_at if query is not None else None,
                last_success_at=query.last_success_at if query is not None else None,
                last_error="another worker is refreshing this query",
                refresh_failed=False,
                counts={"cached": len(postings)},
            )

        query = store.upsert_query(
            source=source,
            fingerprint=fingerprint,
            raw_params=normalized,
            max_pages=max_pages,
        )

        # Phase 19.5: no TTL re-check inside the lock. The whole point
        # of this refactor is that every search hits upstream so the
        # window between two scrapes doesn't hide new postings (D029).
        run_started_at = datetime.now(UTC)
        try:
            scraped = fetch_fn()
            if inspect.isawaitable(scraped):
                scraped = await scraped
            scraped_list = list(scraped)
        except Exception as exc:  # noqa: BLE001 -- bounded; we surface the message
            logger.warning("Search scrape failed for %s: %s", fingerprint[:12], exc)
            store.mark_query_run(query, status="stale", error=str(exc))
            postings = store.get_results(query.id)
            return SearchOutcome(
                postings=postings,
                cached=bool(postings),
                stale=True,
                query_id=query.id,
                last_run_at=query.last_run_at,
                last_success_at=query.last_success_at,
                last_error=str(exc),
                refresh_failed=True,
                counts={"cached": len(postings), "scraped": 0},
            )

        # Persist scraped postings + (re-)link to this query. Every
        # ``link_result`` call stamps ``last_seen_at = now()`` on the
        # link row; we then prune links whose ``last_seen_at`` is
        # older than ``run_started_at`` so postings that disappeared
        # from the source between runs don't keep replaying via the
        # next cache hit (codex P2). The JobPosting row itself is
        # kept -- other queries / applications may still reference
        # it; only the link from this query is removed. The Phase 14
        # ``cache_eviction`` job is what eventually archives an
        # orphaned posting.
        new_count = 0
        kept_postings: list[Any] = []
        for rank, item in enumerate(scraped_list):
            posting = store.upsert_posting(
                source=item.source,
                source_id=item.source_id,
                company=item.company,
                canonical_url=getattr(item, "application_url", None),
            )
            link = store.link_result(
                query_id=query.id, posting_id=posting.id, rank=rank
            )
            if link.first_seen_at == link.last_seen_at:
                new_count += 1
            kept_postings.append(posting)

        removed_count = store.prune_results_not_seen_since(
            query_id=query.id, threshold=run_started_at
        )
        store.mark_query_run(query, status="fresh", result_count=len(scraped_list))
        return SearchOutcome(
            postings=kept_postings,
            cached=False,
            stale=False,
            query_id=query.id,
            last_run_at=query.last_run_at,
            last_success_at=query.last_success_at,
            last_error=None,
            refresh_failed=False,
            counts={
                "scraped": len(scraped_list),
                "new": new_count,
                "removed": removed_count,
            },
        )


class _NullLock:
    """Stand-in used when the caller passes ``cache=None`` (tests, CLI scripts).

    Mirrors the ``AcquiredLock`` context-manager surface but always
    reports ``acquired=True`` so the search flow takes the scrape path.
    """

    acquired = True
    scope = "none"

    def __enter__(self) -> _NullLock:
        return self

    def __exit__(self, *exc: object) -> None:
        return None
