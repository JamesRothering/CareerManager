"""TE-1.2: merge LinkedIn archive chunks (zip or LI_eater CSV) into Postgres."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_db_url, load_config
from src.core.models import (
    LinkedInExportFile,
    LinkedInExportRow,
    LinkedInExportRun,
    LinkedInNetwork,
)
from src.memory.linkedin_eater import eat_linkedin_sources, write_li_csv

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "linkedin"
TENANT = "test-te12-archive"


@pytest.fixture
def db_session():
    engine = create_engine(get_db_url(load_config()))
    session = sessionmaker(bind=engine)()
    yield session
    session.execute(delete(LinkedInExportRow).where(LinkedInExportRow.tenant_id == TENANT))
    session.execute(delete(LinkedInExportFile).where(LinkedInExportFile.tenant_id == TENANT))
    session.execute(delete(LinkedInExportRun).where(LinkedInExportRun.tenant_id == TENANT))
    session.execute(delete(LinkedInNetwork).where(LinkedInNetwork.tenant_id == TENANT))
    session.commit()
    session.close()


def test_import_eaten_csv_and_later_chunk_merges(db_session: Session, tmp_path: Path) -> None:
    from src.memory.linkedin_archive import import_linkedin_archive

    first_zip = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(first_zip, "w") as zf:
        zf.write(FIXTURES / "Connections.csv", "Connections.csv")
        zf.write(FIXTURES / "Profile.csv", "Profile.csv")
    eaten = tmp_path / "LI-2026-08-18.csv"
    write_li_csv(eaten, eat_linkedin_sources([first_zip]))

    first = import_linkedin_archive(db_session, eaten, tenant_id=TENANT)
    assert first.inserted >= 3
    connections = db_session.query(LinkedInNetwork).filter_by(tenant_id=TENANT, kind="connection")
    assert connections.count() == 2
    profile = (
        db_session.query(LinkedInExportRow)
        .filter_by(tenant_id=TENANT, relative_path="Profile.csv")
        .one()
    )
    assert profile.payload["Last Name"] == "Rothering"

    second_zip = tmp_path / "Complete_LinkedInDataExport.zip"
    later_profile = tmp_path / "Profile.csv"
    later_profile.write_text(
        "First Name,Last Name,Maiden Name,Created Date,Address,Industry\n"
        "James,Updated,,01 Jan 2010,,Software\n",
        encoding="utf-8",
    )
    with zipfile.ZipFile(second_zip, "w") as zf:
        zf.write(FIXTURES / "messages.csv", "messages.csv")
        zf.write(later_profile, "Profile.csv")

    second = import_linkedin_archive(db_session, second_zip, tenant_id=TENANT)
    assert second.inserted >= 1
    assert second.updated >= 1
    assert (
        db_session.query(LinkedInExportRow)
        .filter_by(tenant_id=TENANT, relative_path="Connections.csv")
        .count()
        >= 2
    )
    assert (
        db_session.query(LinkedInExportRow)
        .filter_by(tenant_id=TENANT, relative_path="messages.csv")
        .count()
        == 1
    )
    profile = (
        db_session.query(LinkedInExportRow)
        .filter_by(tenant_id=TENANT, relative_path="Profile.csv")
        .one()
    )
    assert profile.payload["Last Name"] == "Updated"
    leftover = db_session.query(LinkedInNetwork).filter_by(tenant_id=TENANT, kind="connection")
    assert leftover.count() == 2


def test_same_chunk_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    from src.memory.linkedin_archive import import_linkedin_archive

    zip_path = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(FIXTURES / "Connections.csv", "Connections.csv")
    first = import_linkedin_archive(db_session, zip_path, tenant_id=TENANT)
    second = import_linkedin_archive(db_session, zip_path, tenant_id=TENANT)
    assert first.inserted >= 2
    assert second.already_imported is True
    assert db_session.query(LinkedInExportRun).filter_by(tenant_id=TENANT).count() == 1
    leftover = db_session.query(LinkedInNetwork).filter_by(tenant_id=TENANT, kind="connection")
    assert leftover.count() == 2
