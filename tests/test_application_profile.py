"""Regression tests for src.application.profile.

The legacy-migration branch in ``_ensure_profile_store`` used to fire on
every call as long as ``data/profile/profile.yaml`` (the legacy
single-file layout) was on disk and ``profiles/default.yaml`` was
missing. That made it impossible to rename or delete the ``default``
profile: the file would simply be re-copied from the legacy path on the
next ``load_profile_data`` call. These tests pin that behavior to
"one-shot, only when profiles/ is empty, and then unlink the legacy
file."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.application import profile as profile_module


@pytest.fixture()
def profile_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module's path constants to a fresh tmp dir."""
    profile_dir = tmp_path / "profile"
    profiles_dir = profile_dir / "profiles"
    legacy_file = profile_dir / "profile.yaml"
    active_file = profile_dir / "active_profile.txt"

    monkeypatch.setattr(profile_module, "PROFILE_DIR", profile_dir)
    monkeypatch.setattr(profile_module, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(profile_module, "LEGACY_PROFILE_FILE", legacy_file)
    monkeypatch.setattr(profile_module, "ACTIVE_PROFILE_FILE", active_file)
    return profile_dir


def _write_legacy(profile_dir: Path, body: str = "identity:\n  full_name: Legacy\n") -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(body, encoding="utf-8")


def test_legacy_migration_runs_once_and_removes_legacy_file(profile_store: Path) -> None:
    _write_legacy(profile_store)

    profile_module._ensure_profile_store()

    assert (profile_store / "profiles" / "default.yaml").exists()
    # Legacy file should be gone after a successful one-shot migration.
    assert not (profile_store / "profile.yaml").exists()


def test_delete_default_profile_does_not_resurrect_from_legacy(profile_store: Path) -> None:
    """The original bug: deleting ``default`` left ``profile.yaml`` on
    disk, and the next ``load_profile_data`` call re-copied it as
    ``profiles/default.yaml``, making the profile undeletable."""
    _write_legacy(profile_store)
    profile_module._ensure_profile_store()  # migrate
    assert (profile_store / "profiles" / "default.yaml").exists()

    result = profile_module.delete_profile_data(profile_id="default")
    assert result["ok"] is True

    # Trigger another store-ensure cycle (this is what re-resurrected
    # the profile before the fix).
    profile_module._ensure_profile_store()
    assert not (profile_store / "profiles" / "default.yaml").exists()
    ids = {p["id"] for p in profile_module.list_profiles()}
    assert "default" not in ids


def test_rename_default_profile_does_not_leave_phantom_default(profile_store: Path) -> None:
    """The reported bug: renaming the default profile created a new
    file with the new name but the original ``default`` came back."""
    _write_legacy(profile_store)
    profile_module._ensure_profile_store()

    result = profile_module.rename_profile_data(
        profile_id="default", new_profile_id="my-resume"
    )
    assert result["ok"] is True

    ids = {p["id"] for p in profile_module.list_profiles()}
    assert ids == {"my-resume"}, f"Phantom default profile resurrected: {ids}"


def test_record_experience_persists_and_returns_by_id(profile_store: Path) -> None:
    profile_module.create_empty_profile(profile_id="corpus", set_active=True)
    result = profile_module.record_experience(
        profile_id="corpus",
        payload={
            "client": "Wells Fargo",
            "role": "Staff Engineer",
            "start_date": "2024-01",
            "end_date": "2025-06",
            "problem": "Ledger close was late every month",
            "actions": ["Automated recon"],
            "outcomes": ["Close in 2 days"],
            "skills": ["Python", "SQL"],
        },
    )
    assert result["ok"] is True
    exp_id = result["experience"]["id"]
    assert result["experience"]["company"] == "Wells Fargo"
    fetched = profile_module.get_experience(profile_id="corpus", experience_id=exp_id)
    assert fetched["ok"] is True
    assert fetched["experience"]["id"] == exp_id
    assert fetched["experience"]["title"] == "Staff Engineer"
    stories = fetched["profile"]["story_bank"]
    assert stories[-1]["context"] == "Ledger close was late every month"


def test_record_experience_rejects_missing_client_or_dates(profile_store: Path) -> None:
    profile_module.create_empty_profile(profile_id="corpus", set_active=True)
    missing_client = profile_module.record_experience(
        profile_id="corpus",
        payload={"start_date": "2024-01", "end_date": "2024-12", "role": "Engineer"},
    )
    assert missing_client["ok"] is False
    assert missing_client["error_code"] == "field_error"
    assert "company" in missing_client["field_errors"]

    missing_dates = profile_module.record_experience(
        profile_id="corpus",
        payload={"company": "Acme", "role": "Engineer"},
    )
    assert missing_dates["ok"] is False
    assert "start_date" in missing_dates["field_errors"]


def test_tag_experience_add_and_remove(profile_store: Path) -> None:
    profile_module.create_empty_profile(profile_id="corpus", set_active=True)
    recorded = profile_module.record_experience(
        profile_id="corpus",
        payload={
            "client": "Wells Fargo",
            "role": "Staff Engineer",
            "start_date": "2024-01",
            "skills": ["Python"],
        },
    )
    exp_id = recorded["experience"]["id"]

    tagged = profile_module.tag_experience(
        profile_id="corpus",
        experience_id=exp_id,
        add_skills=["SQL", "Python"],
        add_domains=["Payments"],
        remove_skills=["Python"],
    )
    assert tagged["ok"] is True
    assert tagged["experience"]["skills"] == ["SQL"]
    assert tagged["experience"]["domains"] == ["Payments"]

    fetched = profile_module.get_experience(profile_id="corpus", experience_id=exp_id)
    assert fetched["experience"]["skills"] == ["SQL"]
    assert fetched["experience"]["domains"] == ["Payments"]


def test_query_experiences_by_tag_returns_only_matches(profile_store: Path) -> None:
    profile_module.create_empty_profile(profile_id="corpus", set_active=True)
    wells = profile_module.record_experience(
        profile_id="corpus",
        payload={"client": "Wells Fargo", "role": "Engineer", "start_date": "2024-01"},
    )["experience"]["id"]
    hulu = profile_module.record_experience(
        profile_id="corpus",
        payload={"client": "Hulu", "role": "Engineer", "start_date": "2023-01"},
    )["experience"]["id"]
    profile_module.tag_experience(
        profile_id="corpus",
        experience_id=wells,
        add_skills=["Python"],
        add_domains=["Banking"],
    )
    profile_module.tag_experience(
        profile_id="corpus",
        experience_id=hulu,
        add_skills=["Vue"],
        add_domains=["Streaming"],
    )

    python_hits = profile_module.query_experiences_by_tag(profile_id="corpus", tag="Python")
    assert python_hits["ok"] is True
    assert [item["id"] for item in python_hits["experiences"]] == [wells]

    banking_hits = profile_module.query_experiences_by_tag(profile_id="corpus", tag="Banking")
    assert [item["id"] for item in banking_hits["experiences"]] == [wells]

    missing = profile_module.query_experiences_by_tag(profile_id="corpus", tag="Rust")
    assert missing["experiences"] == []


def test_unknown_experience_tags_are_stored_not_dropped(profile_store: Path) -> None:
    profile_module.create_empty_profile(profile_id="corpus", set_active=True)
    exp_id = profile_module.record_experience(
        profile_id="corpus",
        payload={"client": "Codojo", "role": "Owner", "start_date": "2020-01"},
    )["experience"]["id"]

    tagged = profile_module.tag_experience(
        profile_id="corpus",
        experience_id=exp_id,
        add_skills=["MadeUpSkillXYZ"],
        add_domains=["NotACatalogDomain"],
    )
    assert tagged["ok"] is True
    assert "MadeUpSkillXYZ" in tagged["experience"]["skills"]
    assert "NotACatalogDomain" in tagged["experience"]["domains"]
    catalog = tagged["profile"]["skills"]
    assert "MadeUpSkillXYZ" not in catalog.get("languages", [])
    fetched = profile_module.get_experience(profile_id="corpus", experience_id=exp_id)
    assert fetched["experience"]["skills"] == ["MadeUpSkillXYZ"]
    assert fetched["experience"]["domains"] == ["NotACatalogDomain"]
