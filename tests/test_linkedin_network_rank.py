"""US-8.1: rank linkedin_network for prune-vs-keep using Messages when present."""

from __future__ import annotations

import zipfile
from datetime import date
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
from src.memory.linkedin_archive import import_linkedin_archive
from src.memory.linkedin_network import import_linkedin_csv
from src.memory.linkedin_rank import rank_for_prune

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "linkedin"
TENANT = "test-us81-rank"


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


def test_rank_orders_worst_first_with_reasons(db_session: Session) -> None:
    import_linkedin_csv(
        db_session, FIXTURES / "Connections.csv", kind="connection", tenant_id=TENANT
    )
    ranked = rank_for_prune(db_session, tenant_id=TENANT, as_of=date(2026, 8, 17))
    assert ranked
    assert ranked[0].prune_score >= ranked[-1].prune_score
    worst = next(r for r in ranked if r.last_name == "Doe")
    assert worst.reasons


def test_never_messaged_ranks_worse_than_messaged(db_session: Session, tmp_path: Path) -> None:
    archive = tmp_path / "chunk.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(FIXTURES / "Connections.csv", "Connections.csv")
        zf.write(FIXTURES / "messages.csv", "messages.csv")
    import_linkedin_archive(db_session, archive, tenant_id=TENANT)
    ranked = rank_for_prune(db_session, tenant_id=TENANT, as_of=date(2026, 8, 17))
    by_name = {r.last_name: r for r in ranked}
    ada = by_name["Lovelace"]
    john = by_name["Doe"]
    assert any("message" in reason.lower() for reason in ada.reasons)
    assert any("no messages" in reason.lower() for reason in john.reasons)
    assert john.prune_score > ada.prune_score


def test_network_cli_help_lists_rank() -> None:
    from click.testing import CliRunner

    from src.cli.main import cli

    result = CliRunner().invoke(cli, ["network", "rank", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.output
