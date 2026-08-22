"""US-8.2: review prune suggestions in the web GUI (keep / kill / later)."""

from __future__ import annotations

import importlib
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_db_url, load_config
from src.core.models import LinkedInNetwork
from src.memory.linkedin_network import import_linkedin_csv
from src.memory.linkedin_rank import rank_for_prune

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "linkedin"
TENANT = "test-us82-gui"
_DECISIONS_MIGRATION_GLOB = "*us_8_2_linkedin_network_decisions*.py"
_HEAD = "b4e7c9a1d2f0"

models = importlib.import_module("src.core.models")


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


def _purge_tenant(session: Session) -> None:
    try:
        from src.core.models import LinkedInNetworkDecision  # noqa: PLC0415

        session.execute(
            delete(LinkedInNetworkDecision).where(LinkedInNetworkDecision.tenant_id == TENANT)
        )
    except Exception:
        session.rollback()
    session.execute(delete(LinkedInNetwork).where(LinkedInNetwork.tenant_id == TENANT))
    session.commit()


@pytest.fixture
def db_session():
    engine = create_engine(get_db_url(load_config()))
    session = sessionmaker(bind=engine)()
    _purge_tenant(session)
    yield session
    _purge_tenant(session)
    session.close()


@pytest.fixture
def client():
    from src.web.app import create_app

    return TestClient(create_app())


def _headers() -> dict[str, str]:
    return {"x-autoapply-tenant": TENANT}


def test_linkedin_network_decision_table_registered() -> None:
    cls = models.LinkedInNetworkDecision
    assert cls.__tablename__ == "linkedin_network_decisions"
    columns = cls.__table__.columns
    for column in (
        "tenant_id",
        "identity_key",
        "kind",
        "decision",
        "decided_at",
    ):
        assert column in columns
    assert columns["tenant_id"].nullable is False
    assert columns["decision"].nullable is False


def test_linkedin_network_decision_upsert_key() -> None:
    unique = _unique_column_sets(models.LinkedInNetworkDecision.__table__)
    assert ("identity_key", "kind", "tenant_id") in unique


def test_linkedin_network_decision_values_are_keep_kill_later() -> None:
    sql_texts = []
    for constraint in models.LinkedInNetworkDecision.__table__.constraints:
        if type(constraint).__name__ != "CheckConstraint":
            continue
        sql_texts.append(str(constraint.sqltext))
    joined = " ".join(sql_texts)
    assert "keep" in joined
    assert "kill" in joined
    assert "later" in joined


def test_us_8_2_decisions_migration_chains_off_archive_head() -> None:
    versions = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    candidates = list(versions.glob(_DECISIONS_MIGRATION_GLOB))
    assert candidates, "US-8.2 decisions migration missing under migrations/versions/"
    content = candidates[0].read_text(encoding="utf-8")
    match = re.search(r"down_revision[^=]*=\s*['\"]([^'\"]+)['\"]", content)
    assert match is not None
    assert match.group(1) == _HEAD
    assert "linkedin_network_decisions" in content


def test_empty_network_returns_import_hint(db_session: Session) -> None:
    from src.application.network import list_prune_suggestions

    payload = list_prune_suggestions(db_session, tenant_id=TENANT)
    assert payload["items"] == []
    assert payload["empty"] is True
    assert "import" in payload["hint"].lower()


def test_suggestions_match_ranker_worst_first(db_session: Session) -> None:
    from src.application.network import list_prune_suggestions

    import_linkedin_csv(
        db_session, FIXTURES / "Connections.csv", kind="connection", tenant_id=TENANT
    )
    ranked = rank_for_prune(db_session, tenant_id=TENANT, as_of=date(2026, 8, 17))
    payload = list_prune_suggestions(
        db_session, tenant_id=TENANT, as_of=date(2026, 8, 17)
    )
    assert payload["empty"] is False
    items = payload["items"]
    assert [row["identity_key"] for row in items] == [row.identity_key for row in ranked]
    assert [row["prune_score"] for row in items] == [row.prune_score for row in ranked]
    assert items[0]["reasons"] == list(ranked[0].reasons)
    doe = next(row for row in items if row["last_name"] == "Doe")
    assert doe["company"] == "Acme"
    assert doe["position"] == "Engineer"
    assert doe["decision"] is None


def test_keep_kill_later_persist_and_survive_rerank(db_session: Session) -> None:
    from src.application.network import list_prune_suggestions, set_network_decision

    import_linkedin_csv(
        db_session, FIXTURES / "Connections.csv", kind="connection", tenant_id=TENANT
    )
    payload = list_prune_suggestions(db_session, tenant_id=TENANT)
    target = next(row for row in payload["items"] if row["last_name"] == "Doe")
    saved = set_network_decision(
        db_session,
        tenant_id=TENANT,
        identity_key=target["identity_key"],
        kind=target["kind"],
        decision="kill",
    )
    assert saved["decision"] == "kill"

    after = list_prune_suggestions(db_session, tenant_id=TENANT)
    marked = next(row for row in after["items"] if row["identity_key"] == target["identity_key"])
    assert marked["decision"] == "kill"
    assert marked["prune_score"] == target["prune_score"]

    suggested = list_prune_suggestions(db_session, tenant_id=TENANT, decision="suggested")
    assert all(row["identity_key"] != target["identity_key"] for row in suggested["items"])
    killed = list_prune_suggestions(db_session, tenant_id=TENANT, decision="kill")
    assert [row["identity_key"] for row in killed["items"]] == [target["identity_key"]]

    still = (
        db_session.query(LinkedInNetwork)
        .filter_by(
            tenant_id=TENANT,
            identity_key=target["identity_key"],
            kind=target["kind"],
        )
        .one()
    )
    assert still.last_name == "Doe"


def test_api_empty_and_decide_round_trip(client: TestClient, db_session: Session) -> None:
    empty = client.get("/api/network/suggestions", headers=_headers())
    assert empty.status_code == 200
    body = empty.json()
    assert body["items"] == []
    assert body["empty"] is True

    import_linkedin_csv(
        db_session, FIXTURES / "Connections.csv", kind="connection", tenant_id=TENANT
    )

    listed = client.get("/api/network/suggestions", headers=_headers())
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert items
    assert items[0]["prune_score"] >= items[-1]["prune_score"]
    target = next(row for row in items if row["last_name"] == "Lovelace")

    decided = client.post(
        "/api/network/decisions",
        headers=_headers(),
        json={
            "identity_key": target["identity_key"],
            "kind": target["kind"],
            "decision": "keep",
        },
    )
    assert decided.status_code == 200
    assert decided.json()["decision"] == "keep"

    keep_tab = client.get(
        "/api/network/suggestions",
        headers=_headers(),
        params={"decision": "keep"},
    )
    assert [row["last_name"] for row in keep_tab.json()["items"]] == ["Lovelace"]

    suggested = client.get(
        "/api/network/suggestions",
        headers=_headers(),
        params={"decision": "suggested"},
    )
    assert all(row["last_name"] != "Lovelace" for row in suggested.json()["items"])

    remaining = (
        db_session.query(LinkedInNetwork)
        .filter_by(tenant_id=TENANT, identity_key=target["identity_key"])
        .count()
    )
    assert remaining == 1


def test_api_rejects_unknown_decision(client: TestClient, db_session: Session) -> None:
    import_linkedin_csv(
        db_session, FIXTURES / "Connections.csv", kind="connection", tenant_id=TENANT
    )
    payload = client.get("/api/network/suggestions", headers=_headers()).json()
    target = payload["items"][0]
    response = client.post(
        "/api/network/decisions",
        headers=_headers(),
        json={
            "identity_key": target["identity_key"],
            "kind": target["kind"],
            "decision": "unfriend",
        },
    )
    assert response.status_code == 400
