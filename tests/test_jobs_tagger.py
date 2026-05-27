"""Phase 19.2: tagger rule tests.

Pin every supported A1 tag against representative JD shapes. The tagger
is profile-independent and must never emit subjective labels (D029); a
regression test guards that boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.jobs.tagger import (
    SUPPORTED_TAGS,
    TAGGER_VERSION,
    SnapshotView,
    compute_tags,
)


def _view(**kw) -> SnapshotView:
    return SnapshotView(**kw)


def test_supported_tags_keys_are_emitted():
    tags = compute_tags(_view(title="Software Engineer"))
    assert set(tags.keys()) == set(SUPPORTED_TAGS)


def test_tagger_version_is_positive():
    assert isinstance(TAGGER_VERSION, int)
    assert TAGGER_VERSION >= 1


@pytest.mark.parametrize(
    "location,description,expected",
    [
        ("Remote - USA", "", "remote"),
        ("", "This is a fully remote role with WFH flexibility.", "remote"),
        ("New York, NY (Hybrid)", "", "hybrid"),
        ("San Francisco, CA", "Hybrid 3 days in office", "hybrid"),
        ("San Francisco, CA", "On-site collaboration required.", "onsite"),
        ("", "", "unknown"),
    ],
)
def test_work_mode(location, description, expected):
    assert compute_tags(_view(location=location, description=description))["work_mode"] == expected


@pytest.mark.parametrize(
    "title,seniority,expected",
    [
        ("Software Engineering Intern", None, "internship"),
        ("Senior Backend Engineer", None, "senior"),
        ("Staff Software Engineer, Infrastructure", None, "staff"),
        ("Backend Engineer II", None, "mid"),
        ("Backend Engineer, Junior", None, "entry"),
        ("Engineer", "senior", "senior"),
        ("Engineer", None, "unknown"),
    ],
)
def test_level(title, seniority, expected):
    assert compute_tags(_view(title=title, seniority=seniority))["level"] == expected


def test_sponsorship_not_offered():
    desc = "Applicants must be authorized to work in the US. We do not sponsor visas."
    assert compute_tags(_view(description=desc))["sponsorship_signal"] == "not_offered"


def test_sponsorship_offered():
    desc = "Visa sponsorship available for qualified candidates."
    assert compute_tags(_view(description=desc))["sponsorship_signal"] == "offered"


def test_sponsorship_silent():
    desc = "We are a fast-moving startup."
    assert compute_tags(_view(description=desc))["sponsorship_signal"] == "silent"


def test_intern_eligible_from_title():
    tags = compute_tags(_view(title="Summer 2026 Software Engineering Intern"))
    assert tags["intern_eligible"] is True
    assert tags["level"] == "internship"


def test_intern_eligible_false_for_senior():
    tags = compute_tags(_view(title="Senior Engineer", description="We hire full-time engineers."))
    assert tags["intern_eligible"] is False


def test_clearance_required():
    desc = "Active TS/SCI clearance with polygraph required."
    assert compute_tags(_view(description=desc))["clearance_required"] is True


def test_clearance_not_required():
    assert compute_tags(_view(description="No clearance needed."))["clearance_required"] is False


def test_usa_only_explicit():
    desc = "Candidates must be US citizens only."
    assert compute_tags(_view(description=desc))["usa_only"] is True


def test_usa_location_alone_is_not_usa_only():
    tags = compute_tags(_view(location="New York, NY, USA", description="Remote-friendly team."))
    assert tags["usa_only"] is False


def test_age_bucket_fresh():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    view = _view(scraped_at=now - timedelta(hours=3))
    assert compute_tags(view, now=now)["posting_age_bucket"] == "fresh_24h"


def test_age_bucket_week():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    view = _view(scraped_at=now - timedelta(days=3))
    assert compute_tags(view, now=now)["posting_age_bucket"] == "week"


def test_age_bucket_older():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    view = _view(scraped_at=now - timedelta(days=60))
    assert compute_tags(view, now=now)["posting_age_bucket"] == "older"


def test_age_bucket_unknown_without_timestamp():
    assert compute_tags(_view())["posting_age_bucket"] == "unknown"


def test_tagger_is_profile_independent():
    """D029: tagger must never read profile data; signature should not
    accept one. This test pins the contract."""
    import inspect

    sig = inspect.signature(compute_tags)
    assert "profile" not in sig.parameters
    assert "applicant" not in sig.parameters


def test_no_subjective_tags_emitted():
    """D029: A1 must not produce ``good_match`` / ``worth_applying`` /
    ``high_priority``. Those belong to A2 score."""
    tags = compute_tags(_view(title="Senior Software Engineer", description="Great team!"))
    for forbidden in ("good_match", "worth_applying", "high_priority"):
        assert forbidden not in tags
