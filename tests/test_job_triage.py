"""US-2.4 — Pursue / Skip / Later on a job.

Acceptance: status persists; the list filters by status.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import sessionmaker

from src.application.job_triage import (
    VALID_STATUSES,
    JobTriageError,
    job_triage_key,
    list_job_triage,
    set_job_triage,
)
from src.core.config import get_db_url, load_config
from src.core.models import JobTriage

TENANT = "test-us24-triage"


def test_job_triage_key_prefers_source_and_source_id() -> None:
    assert (
        job_triage_key({"source": "linkedin", "source_id": "abc", "id": "uuid-1"})
        == "linkedin::abc"
    )


def test_job_triage_key_falls_back_to_id() -> None:
    assert job_triage_key({"id": "uuid-1"}) == "uuid-1"


def test_job_triage_model_registered() -> None:
    assert JobTriage.__tablename__ == "job_triage"
    columns = JobTriage.__table__.columns
    for name in ("tenant_id", "job_key", "status", "job"):
        assert name in columns
    constraint_names = {constraint.name for constraint in JobTriage.__table__.constraints}
    assert "uq_job_triage_tenant_job_key" in constraint_names
    assert "ck_job_triage_status" in constraint_names


@pytest.fixture
def db_session():
    try:
        engine = create_engine(get_db_url(load_config()))
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        JobTriage.__table__.create(engine, checkfirst=True)
    except Exception as exc:  # noqa: BLE001 — skip when CI/local postgres is down
        pytest.skip(f"postgres unavailable: {exc}")
    session = sessionmaker(bind=engine)()
    session.execute(delete(JobTriage).where(JobTriage.tenant_id == TENANT))
    session.commit()
    yield session
    session.execute(delete(JobTriage).where(JobTriage.tenant_id == TENANT))
    session.commit()
    session.close()


def test_status_persists_and_list_filters_by_status(db_session) -> None:
    skip_job = {
        "id": "job-skip",
        "source": "greenhouse",
        "source_id": "skip-1",
        "company": "Acme",
        "title": "Painter",
    }
    pursue_job = {
        "id": "job-pursue",
        "source": "linkedin",
        "source_id": "pursue-1",
        "company": "TransUnion",
        "title": "Fraud Engineer",
    }
    set_job_triage(db_session, job=skip_job, status="skip", tenant_id=TENANT)
    set_job_triage(db_session, job=pursue_job, status="pursue", tenant_id=TENANT)

    again = set_job_triage(db_session, job=pursue_job, status="later", tenant_id=TENANT)
    assert again["status"] == "later"

    skipped = list_job_triage(db_session, tenant_id=TENANT, status="skip")
    assert [item["job_key"] for item in skipped["items"]] == ["greenhouse::skip-1"]
    later = list_job_triage(db_session, tenant_id=TENANT, status="later")
    assert [item["job_key"] for item in later["items"]] == ["linkedin::pursue-1"]
    everything = list_job_triage(db_session, tenant_id=TENANT, status="all")
    assert everything["counts"]["skip"] == 1
    assert everything["counts"]["later"] == 1
    assert everything["counts"]["pursue"] == 0


def test_invalid_status_rejected(db_session) -> None:
    with pytest.raises(JobTriageError):
        set_job_triage(
            db_session,
            job={"id": "x", "source": "ats", "source_id": "1"},
            status="maybe",
            tenant_id=TENANT,
        )
    assert "pursue" in VALID_STATUSES


@pytest.fixture
def client() -> TestClient:
    from src.web.app import create_app

    return TestClient(create_app())


class TestJobTriageRoute:
    def test_list_and_set(self, client: TestClient) -> None:
        listed = {
            "ok": True,
            "status": "skip",
            "items": [
                {
                    "job_key": "linkedin::abc",
                    "status": "skip",
                    "job": {"title": "Painter"},
                }
            ],
            "counts": {"pursue": 0, "skip": 1, "later": 0, "all": 1},
        }
        with patch(
            "src.web.routes.api.list_job_triage_usecase",
            return_value=listed,
        ):
            response = client.get("/api/jobs/triage", params={"status": "skip"})
        assert response.status_code == 200
        assert response.json()["items"][0]["status"] == "skip"

        saved = {
            "ok": True,
            "job_key": "linkedin::abc",
            "status": "pursue",
            "job": {"title": "Fraud Engineer"},
        }
        with patch(
            "src.web.routes.api.set_job_triage_usecase",
            return_value=saved,
        ) as usecase:
            response = client.post(
                "/api/jobs/triage",
                json={
                    "status": "pursue",
                    "job": {
                        "source": "linkedin",
                        "source_id": "abc",
                        "title": "Fraud Engineer",
                    },
                },
            )
        assert response.status_code == 200
        assert response.json()["status"] == "pursue"
        usecase.assert_called_once()

    def test_set_requires_status(self, client: TestClient) -> None:
        response = client.post("/api/jobs/triage", json={"job": {"id": "x"}})
        assert response.status_code == 422
