"""US-3.1 — log a communication against a job.

Acceptance: job id is required. List-by-job is chronological.
Each row has channel, direction, summary, and next action.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import sessionmaker

from src.application.job_communications import (
    JobCommunicationError,
    list_job_communications,
    log_job_communication,
)
from src.core.config import get_db_url, load_config
from src.core.models import JobCommunication

TENANT = "test-us31-comms"


def test_job_communication_model_registered() -> None:
    assert JobCommunication.__tablename__ == "job_communications"
    columns = JobCommunication.__table__.columns
    for name in (
        "tenant_id",
        "job_id",
        "channel",
        "direction",
        "summary",
        "next_action",
        "occurred_at",
    ):
        assert name in columns


@pytest.fixture
def db_session():
    try:
        engine = create_engine(get_db_url(load_config()))
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        JobCommunication.__table__.create(engine, checkfirst=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable: {exc}")
    session = sessionmaker(bind=engine)()
    session.execute(delete(JobCommunication).where(JobCommunication.tenant_id == TENANT))
    session.commit()
    yield session
    session.execute(delete(JobCommunication).where(JobCommunication.tenant_id == TENANT))
    session.commit()
    session.close()


def test_log_requires_job_id(db_session) -> None:
    with pytest.raises(JobCommunicationError):
        log_job_communication(
            db_session,
            tenant_id=TENANT,
            job_id="",
            channel="email",
            direction="outbound",
            summary="Pinged recruiter",
        )


def test_list_by_job_is_chronological(db_session) -> None:
    later = datetime.now(UTC)
    earlier = later - timedelta(hours=2)
    log_job_communication(
        db_session,
        tenant_id=TENANT,
        job_id="job-1",
        channel="email",
        direction="outbound",
        summary="Sent resume",
        next_action="Wait for reply",
        occurred_at=later,
    )
    log_job_communication(
        db_session,
        tenant_id=TENANT,
        job_id="job-1",
        channel="phone",
        direction="inbound",
        summary="Recruiter called",
        next_action="Send calendar",
        occurred_at=earlier,
    )
    log_job_communication(
        db_session,
        tenant_id=TENANT,
        job_id="job-other",
        channel="linkedin",
        direction="outbound",
        summary="InMail",
        occurred_at=earlier,
    )

    listed = list_job_communications(db_session, tenant_id=TENANT, job_id="job-1")
    assert listed["ok"] is True
    summaries = [item["summary"] for item in listed["items"]]
    assert summaries == ["Recruiter called", "Sent resume"]
    first = listed["items"][0]
    assert first["channel"] == "phone"
    assert first["direction"] == "inbound"
    assert first["next_action"] == "Send calendar"


@pytest.fixture
def client() -> TestClient:
    from src.web.app import create_app

    return TestClient(create_app())


class TestJobCommunicationsRoute:
    def test_post_and_list(self, client: TestClient) -> None:
        saved = {
            "ok": True,
            "item": {
                "id": "c1",
                "job_id": "job-1",
                "channel": "email",
                "direction": "outbound",
                "summary": "Sent resume",
                "next_action": "Wait",
            },
        }
        with patch(
            "src.web.routes.api.log_job_communication_usecase",
            return_value=saved,
        ):
            response = client.post(
                "/api/jobs/job-1/communications",
                json={
                    "channel": "email",
                    "direction": "outbound",
                    "summary": "Sent resume",
                    "next_action": "Wait",
                },
            )
        assert response.status_code == 200
        assert response.json()["item"]["summary"] == "Sent resume"

        listed = {
            "ok": True,
            "job_id": "job-1",
            "items": [saved["item"]],
        }
        with patch(
            "src.web.routes.api.list_job_communications_usecase",
            return_value=listed,
        ):
            response = client.get("/api/jobs/job-1/communications")
        assert response.status_code == 200
        assert response.json()["items"][0]["channel"] == "email"

    def test_post_requires_summary(self, client: TestClient) -> None:
        response = client.post(
            "/api/jobs/job-1/communications",
            json={"channel": "email", "direction": "outbound"},
        )
        assert response.status_code == 422
