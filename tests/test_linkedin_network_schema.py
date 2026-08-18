"""TE-1.1: LinkedIn connections/followers tables on the existing Postgres.

Official data-export CSVs land here later (TE-1.2). This story only adds
schema: tenant-scoped rows keyed for idempotent upsert on member URL or email.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

models = importlib.import_module("src.core.models")

_MIGRATION_GLOB = "*te_1_1_linkedin_network*.py"
_HEAD = "c3a7e1f2b048"


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


def test_te_1_1_migration_chains_off_current_head() -> None:
    versions = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    candidates = list(versions.glob(_MIGRATION_GLOB))
    assert candidates, "TE-1.1 migration missing under migrations/versions/"
    content = candidates[0].read_text(encoding="utf-8")
    match = re.search(r"down_revision[^=]*=\s*['\"]([^'\"]+)['\"]", content)
    assert match is not None
    assert match.group(1) == _HEAD
    assert "linkedin_network" in content
    assert "identity_key" in content
