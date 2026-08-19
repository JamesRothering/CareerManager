"""TE-1.1: LinkedIn full-archive tables on the existing Postgres.

The official Complete_LinkedInDataExport zip is many CSVs (and some
JSON/TXT/HTML/media), not only Connections and Followers. Schema holds
every file and every tabular row, plus a typed ``linkedin_network``
projection for prune ranking. Import is TE-1.2.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

models = importlib.import_module("src.core.models")

_NETWORK_MIGRATION_GLOB = "*te_1_1_linkedin_network*.py"
_ARCHIVE_MIGRATION_GLOB = "*te_1_1_linkedin_export_archive*.py"
_HEAD = "c3a7e1f2b048"
_NETWORK_REVISION = "a9f1c4e2d8b0"


def _unique_column_sets(table) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for constraint in table.constraints:
        if not hasattr(constraint, "columns"):
            continue
        name = getattr(constraint, "name", None) or ""
        if name.startswith("uq_") or getattr(constraint, "unique", False):
            found.add(tuple(sorted(c.name for c in constraint.columns)))
    for index in table.indexes:
        if index.unique:
            found.add(tuple(sorted(c.name for c in index.columns)))
    return found


def test_linkedin_network_table_registered() -> None:
    cls = models.LinkedInNetwork
    assert cls.__tablename__ == "linkedin_network"


def test_linkedin_network_carries_tenant_id() -> None:
    columns = models.LinkedInNetwork.__table__.columns
    assert "tenant_id" in columns
    assert columns["tenant_id"].nullable is False


@pytest.mark.parametrize(
    "column",
    (
        "kind",
        "identity_key",
        "profile_url",
        "email",
        "first_name",
        "last_name",
        "company",
        "position",
        "headline",
        "connected_on",
        "raw",
    ),
)
def test_linkedin_network_has_import_columns(column: str) -> None:
    assert column in models.LinkedInNetwork.__table__.columns


def test_linkedin_network_upsert_key_is_tenant_kind_identity() -> None:
    unique = _unique_column_sets(models.LinkedInNetwork.__table__)
    assert ("identity_key", "kind", "tenant_id") in unique


def test_linkedin_network_kind_is_connection_or_follower() -> None:
    sql_texts = []
    for constraint in models.LinkedInNetwork.__table__.constraints:
        if type(constraint).__name__ != "CheckConstraint":
            continue
        sql_texts.append(str(constraint.sqltext))
    joined = " ".join(sql_texts)
    assert "connection" in joined
    assert "follower" in joined


def test_te_1_1_network_migration_chains_off_current_head() -> None:
    versions = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    candidates = list(versions.glob(_NETWORK_MIGRATION_GLOB))
    assert candidates, "TE-1.1 network migration missing under migrations/versions/"
    content = candidates[0].read_text(encoding="utf-8")
    match = re.search(r"down_revision[^=]*=\s*['\"]([^'\"]+)['\"]", content)
    assert match is not None
    assert match.group(1) == _HEAD
    assert "linkedin_network" in content
    assert "identity_key" in content


def test_linkedin_export_run_table_registered() -> None:
    assert models.LinkedInExportRun.__tablename__ == "linkedin_export_runs"
    columns = models.LinkedInExportRun.__table__.columns
    for column in ("tenant_id", "source_name", "source_kind", "source_sha256"):
        assert column in columns
    assert columns["tenant_id"].nullable is False


def test_linkedin_export_file_table_registered() -> None:
    assert models.LinkedInExportFile.__tablename__ == "linkedin_export_files"
    columns = models.LinkedInExportFile.__table__.columns
    for column in (
        "run_id",
        "tenant_id",
        "relative_path",
        "media_kind",
        "byte_size",
        "sha256",
        "header",
        "row_count",
    ):
        assert column in columns


def test_linkedin_export_row_stores_json_payload() -> None:
    assert models.LinkedInExportRow.__tablename__ == "linkedin_export_rows"
    columns = models.LinkedInExportRow.__table__.columns
    assert "payload" in columns
    assert "row_index" in columns
    assert "relative_path" in columns


def test_linkedin_export_file_unique_on_run_and_path() -> None:
    unique = _unique_column_sets(models.LinkedInExportFile.__table__)
    assert ("relative_path", "run_id") in unique


def test_linkedin_export_row_unique_on_file_and_index() -> None:
    unique = _unique_column_sets(models.LinkedInExportRow.__table__)
    assert ("file_id", "row_index") in unique


def test_te_1_1_archive_migration_chains_off_network_migration() -> None:
    versions = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    candidates = list(versions.glob(_ARCHIVE_MIGRATION_GLOB))
    assert candidates, "TE-1.1 archive migration missing under migrations/versions/"
    content = candidates[0].read_text(encoding="utf-8")
    match = re.search(r"down_revision[^=]*=\s*['\"]([^'\"]+)['\"]", content)
    assert match is not None
    assert match.group(1) == _NETWORK_REVISION
    assert "linkedin_export_runs" in content
    assert "linkedin_export_files" in content
    assert "linkedin_export_rows" in content
