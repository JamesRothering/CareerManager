"""Phase 19.2: A1 objective tagger for ``JobSnapshot`` rows.

The tagger reads only the JD snapshot --- never the applicant profile ---
and emits a small, deterministic vocabulary of objective attributes the
filter fast-path can short-circuit on. Anything subjective (``good_match``,
``worth_applying``, ``high_priority``) belongs to the A2 score cache, not
here (D029).

Bumping ``TAGGER_VERSION`` is the cache-invalidation knob: the Phase 19.3
``posting.tag_backfill`` task pages through rows where
``tagger_version < TAGGER_VERSION`` and reruns :func:`compute_tags` on each.

The tag set is intentionally small. Adding a new tag means:

  * widening :data:`SUPPORTED_TAGS` so callers can audit the surface,
  * bumping :data:`TAGGER_VERSION`, and
  * adding a unit test that pins the new tag against representative JDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Bump on any rule change. The Phase 19.3 backfill enqueues a paginated
# retag whenever this number moves forward; downgrades are not supported.
TAGGER_VERSION: int = 1

SUPPORTED_TAGS: tuple[str, ...] = (
    "work_mode",  # remote / hybrid / onsite / unknown
    "level",  # internship / entry / mid / senior / staff / unknown
    "sponsorship_signal",  # offered / not_offered / silent
    "intern_eligible",  # bool
    "posting_age_bucket",  # fresh_24h / week / month / older / unknown
    "clearance_required",  # bool
    "usa_only",  # bool
)


@dataclass(frozen=True)
class SnapshotView:
    """Read-only view of the JD a tagger rule sees.

    Decoupled from the ORM so the rules can be unit-tested without a
    database session.  ``scraped_at`` doubles as "when did we observe this
    JD" for the age bucket.
    """

    title: str = ""
    location: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    description: str | None = None
    requirements: dict[str, Any] | None = None
    scraped_at: datetime | None = None


# --- helpers --------------------------------------------------------

_REMOTE_HINTS = (
    "remote",
    "work from home",
    "wfh",
    "fully remote",
    "100% remote",
    "anywhere",
    "distributed team",
)
_HYBRID_HINTS = ("hybrid", "flex", "flexible work", "remote-friendly", "remote friendly")
_ONSITE_HINTS = ("on-site", "onsite", "in office", "in-office", "in person", "in-person")

_INTERN_HINTS = ("intern", "internship", "co-op", "coop")
_SENIOR_HINTS = ("senior", "sr.", "lead", "principal", "manager")
_STAFF_HINTS = ("staff engineer", "staff swe", "staff software")
_MID_HINTS = ("mid-level", "mid level", "ii", "iii")
_ENTRY_HINTS = ("entry-level", "entry level", "new grad", "junior", "jr.")

_SPONSOR_NEG_PATTERNS = (
    re.compile(r"\bno\s+sponsorship\b", re.IGNORECASE),
    re.compile(r"\bdo(?:es)?\s+not\s+sponsor\b", re.IGNORECASE),
    re.compile(r"\bunable\s+to\s+sponsor\b", re.IGNORECASE),
    re.compile(r"\bsponsorship\s+is\s+not\s+(?:available|offered|provided)\b", re.IGNORECASE),
    re.compile(r"\bmust\s+be\s+(?:authorized|eligible)\s+to\s+work\b", re.IGNORECASE),
    re.compile(r"\bno\s+visa(?:\s+sponsorship)?\b", re.IGNORECASE),
)
_SPONSOR_POS_PATTERNS = (
    re.compile(r"\bvisa\s+sponsorship\s+(?:available|provided|offered)\b", re.IGNORECASE),
    re.compile(r"\bwe\s+(?:will\s+)?sponsor\b", re.IGNORECASE),
    re.compile(r"\bh-?1b\s+sponsorship\s+(?:available|provided|offered)\b", re.IGNORECASE),
    re.compile(r"\bsponsorship\s+is\s+(?:available|offered|provided)\b", re.IGNORECASE),
)

_CLEARANCE_PATTERNS = (
    re.compile(r"\b(?:security|active)\s+clearance\b", re.IGNORECASE),
    re.compile(r"\bsecret\s+clearance\b", re.IGNORECASE),
    re.compile(r"\btop\s+secret\b", re.IGNORECASE),
    re.compile(r"\bts/sci\b", re.IGNORECASE),
    re.compile(r"\bpolygraph\b", re.IGNORECASE),
)

_USA_ONLY_PATTERNS = (
    re.compile(r"\bus(?:a)?\s+only\b", re.IGNORECASE),
    re.compile(r"\bunited\s+states\s+only\b", re.IGNORECASE),
    re.compile(
        r"\bmust\s+(?:reside|be\s+located)\s+in\s+the\s+(?:us|usa|united\s+states)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bus\s+citizens?\s+only\b", re.IGNORECASE),
)


def _joined_text(view: SnapshotView) -> str:
    parts = [view.title or "", view.description or ""]
    if view.requirements:
        parts.append(str(view.requirements))
    return "\n".join(parts).lower()


def _any_in(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _classify_work_mode(view: SnapshotView, text: str) -> str:
    location = (view.location or "").lower()
    if "remote" in location:
        return "remote"
    if "hybrid" in location:
        return "hybrid"
    if _any_in(text, _REMOTE_HINTS):
        return "remote"
    if _any_in(text, _HYBRID_HINTS):
        return "hybrid"
    if _any_in(text, _ONSITE_HINTS) or location:
        return "onsite"
    return "unknown"


def _classify_level(view: SnapshotView, text: str) -> str:
    seniority = (view.seniority or "").strip().lower()
    if seniority:
        if seniority in {"internship", "intern"}:
            return "internship"
        if seniority in {"entry", "entry_level", "entry-level", "junior"}:
            return "entry"
        if seniority in {"mid", "mid_level", "mid-level"}:
            return "mid"
        if seniority in {"senior", "sr"}:
            return "senior"
        if seniority in {"staff", "principal"}:
            return "staff"
    if _any_in(text, _INTERN_HINTS):
        return "internship"
    if _any_in(text, _STAFF_HINTS):
        return "staff"
    if _any_in(text, _SENIOR_HINTS):
        return "senior"
    if _any_in(text, _ENTRY_HINTS):
        return "entry"
    if _any_in(text, _MID_HINTS):
        return "mid"
    return "unknown"


def _classify_sponsorship(text: str) -> str:
    if any(p.search(text) for p in _SPONSOR_NEG_PATTERNS):
        return "not_offered"
    if any(p.search(text) for p in _SPONSOR_POS_PATTERNS):
        return "offered"
    return "silent"


def _classify_intern_eligible(view: SnapshotView, text: str, level: str) -> bool:
    if level == "internship":
        return True
    employment = (view.employment_type or "").lower()
    if employment in {"internship", "intern", "coop", "co-op"}:
        return True
    return _any_in(text, _INTERN_HINTS)


def _classify_clearance(text: str) -> bool:
    return any(p.search(text) for p in _CLEARANCE_PATTERNS)


def _classify_usa_only(view: SnapshotView, text: str) -> bool:
    if any(p.search(text) for p in _USA_ONLY_PATTERNS):
        return True
    # Geographic location alone does not prove "USA-only"; many JDs list
    # a USA office while still hiring globally or remotely. Only explicit
    # text patterns above set this hard-rule tag.
    return False


def _classify_age_bucket(view: SnapshotView, now: datetime | None) -> str:
    if view.scraped_at is None:
        return "unknown"
    reference = now or datetime.now(UTC)
    age = reference - view.scraped_at
    if age <= timedelta(hours=24):
        return "fresh_24h"
    if age <= timedelta(days=7):
        return "week"
    if age <= timedelta(days=30):
        return "month"
    return "older"


# --- public API ----------------------------------------------------


def compute_tags(view: SnapshotView, *, now: datetime | None = None) -> dict[str, Any]:
    """Compute the full A1 tag dict for a snapshot.

    Pure function: same input -> same output, no I/O, no profile data.
    """
    text = _joined_text(view)
    level = _classify_level(view, text)
    return {
        "work_mode": _classify_work_mode(view, text),
        "level": level,
        "sponsorship_signal": _classify_sponsorship(text),
        "intern_eligible": _classify_intern_eligible(view, text, level),
        "posting_age_bucket": _classify_age_bucket(view, now),
        "clearance_required": _classify_clearance(text),
        "usa_only": _classify_usa_only(view, text),
    }


def snapshot_view_from_row(row: Any) -> SnapshotView:
    """Build a :class:`SnapshotView` from a SQLAlchemy ``JobSnapshot``.

    Kept here so the task body / tests don't reach into the ORM shape
    directly.  Missing attributes degrade to ``None`` / ``""`` so the
    same helper works with stub objects in tests.
    """
    requirements = getattr(row, "requirements", None)
    if requirements is not None and not isinstance(requirements, dict):
        requirements = None
    return SnapshotView(
        title=getattr(row, "title", "") or "",
        location=getattr(row, "location", None),
        employment_type=getattr(row, "employment_type", None),
        seniority=getattr(row, "seniority", None),
        description=getattr(row, "description", None),
        requirements=requirements,
        scraped_at=getattr(row, "scraped_at", None),
    )


__all__ = [
    "SUPPORTED_TAGS",
    "TAGGER_VERSION",
    "SnapshotView",
    "compute_tags",
    "snapshot_view_from_row",
]
