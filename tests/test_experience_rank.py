"""US-2.2 — rank work experiences against a job.

Acceptance: a JD that mentions Cassandra + fraud + bank ranks TransUnion
and M&T above unrelated experiences. Scores are deterministic on fixtures.
Zero overlap returns an empty list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.application import matching as matching_module
from src.application import profile as profile_module
from src.matching.experience_rank import rank_experiences

_FRAUD_JOB = {
    "title": "Fraud Platform Engineer",
    "description": (
        "We need Cassandra experience for real-time fraud detection at a bank. "
        "You will model identity risk and mule activity."
    ),
}

_TRANSUNION = {
    "id": "exp-transunion",
    "company": "TransUnion",
    "title": "Staff Engineer",
    "skills": ["Cassandra"],
    "domains": ["fraud"],
    "problem": "Identity fraud rings abused credit files",
    "actions": ["Built Cassandra pipelines for fraud detection"],
    "outcomes": ["Caught mule rings faster"],
}

_MT = {
    "id": "exp-mt",
    "company": "M&T",
    "title": "Platform Engineer",
    "skills": ["Java"],
    "domains": ["bank"],
    "problem": "Core bank ledgers were batch-only",
    "actions": ["Moved settlement onto a streaming ledger"],
    "outcomes": ["Same-day posting"],
}

_HULU = {
    "id": "exp-hulu",
    "company": "Hulu",
    "title": "Frontend Engineer",
    "skills": ["Vue"],
    "domains": ["streaming"],
    "problem": "Playback UI jank on living-room devices",
    "actions": ["Rewrote the player shell"],
    "outcomes": ["Smoother bitrate switches"],
}


@pytest.fixture()
def profile_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile_dir = tmp_path / "profile"
    profiles_dir = profile_dir / "profiles"
    legacy_file = profile_dir / "profile.yaml"
    active_file = profile_dir / "active_profile.txt"

    monkeypatch.setattr(profile_module, "PROFILE_DIR", profile_dir)
    monkeypatch.setattr(profile_module, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(profile_module, "LEGACY_PROFILE_FILE", legacy_file)
    monkeypatch.setattr(profile_module, "ACTIVE_PROFILE_FILE", active_file)
    return profile_dir


def test_rank_experiences_orders_transunion_and_mt_above_unrelated() -> None:
    ranked = rank_experiences([_HULU, _MT, _TRANSUNION], _FRAUD_JOB)
    companies = [row["company"] for row in ranked]
    assert companies[:2] == ["TransUnion", "M&T"]
    assert "Hulu" not in companies
    assert ranked[0]["score"] > ranked[1]["score"] > 0


def test_rank_experiences_is_deterministic_on_fixtures() -> None:
    first = rank_experiences([_HULU, _MT, _TRANSUNION], _FRAUD_JOB)
    second = rank_experiences([_TRANSUNION, _HULU, _MT], _FRAUD_JOB)
    assert first == second
    assert [row["matched_terms"] for row in first] == [
        row["matched_terms"] for row in second
    ]


def test_rank_experiences_zero_overlap_returns_empty_list() -> None:
    painter_job = {
        "title": "Studio Painter",
        "description": "Oil portraits, watercolor washes, gallery hanging.",
    }
    assert rank_experiences([_HULU, _MT, _TRANSUNION], painter_job) == []


def test_generic_engineer_title_alone_does_not_count_as_overlap() -> None:
    generic_job = {
        "title": "Senior Software Engineer",
        "description": "Join our team. Software engineering experience required.",
    }
    assert rank_experiences([_HULU], generic_job) == []


def test_rank_experiences_for_job_uses_profile_corpus(profile_store: Path) -> None:
    profile_module.create_empty_profile(profile_id="corpus", set_active=True)
    profile_module.record_experience(
        profile_id="corpus",
        payload={
            "client": "TransUnion",
            "role": "Staff Engineer",
            "start_date": "2019-01",
            "skills": ["Cassandra"],
            "domains": ["fraud"],
            "problem": "Identity fraud rings abused credit files",
            "actions": ["Built Cassandra pipelines for fraud detection"],
        },
    )
    profile_module.record_experience(
        profile_id="corpus",
        payload={
            "client": "M&T",
            "role": "Platform Engineer",
            "start_date": "2017-01",
            "domains": ["bank"],
            "problem": "Core bank ledgers were batch-only",
        },
    )
    profile_module.record_experience(
        profile_id="corpus",
        payload={
            "client": "Hulu",
            "role": "Frontend Engineer",
            "start_date": "2015-01",
            "skills": ["Vue"],
            "domains": ["streaming"],
        },
    )

    result = matching_module.rank_experiences_for_job(
        job=_FRAUD_JOB, profile_id="corpus"
    )
    assert result["ok"] is True
    companies = [row["company"] for row in result["rankings"]]
    assert companies[:2] == ["TransUnion", "M&T"]
    assert "Hulu" not in companies

    again = matching_module.rank_experiences_for_job(
        job=_FRAUD_JOB, profile_id="corpus"
    )
    assert again["rankings"] == result["rankings"]


def test_rank_experiences_for_job_missing_profile(profile_store: Path) -> None:
    result = matching_module.rank_experiences_for_job(
        job=_FRAUD_JOB, profile_id="missing"
    )
    assert result["ok"] is False
    assert result["rankings"] == []
    assert result["error_code"] == "profile_not_found"
