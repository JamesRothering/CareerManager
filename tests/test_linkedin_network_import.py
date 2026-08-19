"""TE-1.2: import official LinkedIn Connections / Followers CSVs.

Fixtures mimic LinkedIn's data archive (notes preamble + header row).
Real files from James may arrive later; tests do not require them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_db_url, load_config
from src.core.models import LinkedInNetwork, TENANT_DEFAULT
from src.memory.linkedin_network import (
    ImportReport,
    identity_key,
    import_linkedin_csv,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "linkedin"
TENANT = "test-te12-network"


@pytest.fixture
def db_session():
    engine = create_engine(get_db_url(load_config()))
    session = sessionmaker(bind=engine)()
    yield session
    session.execute(delete(LinkedInNetwork).where(LinkedInNetwork.tenant_id == TENANT))
    session.commit()
    session.close()


def test_identity_key_prefers_profile_url() -> None:
    assert (
        identity_key(
            "https://www.linkedin.com/in/Ada-Lovelace/",
            "ada@example.com",
        )
        == "https://www.linkedin.com/in/ada-lovelace"
    )


def test_identity_key_falls_back_to_email() -> None:
    assert identity_key(None, " Ada@Example.COM ") == "ada@example.com"


def test_identity_key_rejects_empty() -> None:
    with pytest.raises(ValueError):
        identity_key("  ", None)


def test_import_connections_skips_notes_preamble(db_session: Session) -> None:
    report = import_linkedin_csv(
        db_session,
        FIXTURES / "Connections.csv",
        kind="connection",
        tenant_id=TENANT,
    )
    assert report == ImportReport(inserted=2, updated=0, invalid=1)
    rows = (
        db_session.query(LinkedInNetwork)
        .filter_by(tenant_id=TENANT, kind="connection")
        .order_by(LinkedInNetwork.last_name)
        .all()
    )
    assert [r.last_name for r in rows] == ["Doe", "Lovelace"]
    ada = rows[1]
    assert ada.company == "Analytical Engines"
    assert ada.connected_on == date(2020, 1, 16)
    assert ada.email == "ada@example.com"


def test_import_followers_csv(db_session: Session) -> None:
    report = import_linkedin_csv(
        db_session,
        FIXTURES / "Followers.csv",
        kind="follower",
        tenant_id=TENANT,
    )
    assert report.inserted == 1
    assert report.invalid == 0
    row = db_session.query(LinkedInNetwork).filter_by(tenant_id=TENANT, kind="follower").one()
    assert row.headline == "Staff engineer"
    assert row.identity_key == "https://www.linkedin.com/in/ada-lovelace"


def test_reimport_is_idempotent_and_updates(db_session: Session) -> None:
    first = import_linkedin_csv(
        db_session,
        FIXTURES / "Connections.csv",
        kind="connection",
        tenant_id=TENANT,
    )
    second = import_linkedin_csv(
        db_session,
        FIXTURES / "Connections.csv",
        kind="connection",
        tenant_id=TENANT,
    )
    assert first.inserted == 2
    assert second.inserted == 0
    assert second.updated == 2
    assert second.invalid == 1
    count = db_session.query(LinkedInNetwork).filter_by(tenant_id=TENANT, kind="connection").count()
    assert count == 2


def test_connection_and_follower_same_url_are_separate_rows(db_session: Session) -> None:
    import_linkedin_csv(
        db_session,
        FIXTURES / "Connections.csv",
        kind="connection",
        tenant_id=TENANT,
    )
    import_linkedin_csv(
        db_session,
        FIXTURES / "Followers.csv",
        kind="follower",
        tenant_id=TENANT,
    )
    assert db_session.query(LinkedInNetwork).filter_by(tenant_id=TENANT).count() == 3


def test_network_cli_help_lists_import() -> None:
    from click.testing import CliRunner

    from src.cli.main import cli

    result = CliRunner().invoke(cli, ["network", "import", "--help"])
    assert result.exit_code == 0
    assert "--connections" in result.output
    assert "--followers" in result.output
    assert "--archive" in result.output
