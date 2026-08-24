"""US-2.2 wire-up for ``POST /api/matching/rank-experiences``."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from src.web.app import create_app

    return TestClient(create_app())


class TestRankExperiencesRoute:
    def test_route_returns_rankings(self, client: TestClient) -> None:
        payload = {
            "ok": True,
            "profile_id": "corpus",
            "rankings": [
                {
                    "id": "exp-transunion",
                    "company": "TransUnion",
                    "title": "Staff Engineer",
                    "score": 0.5,
                    "matched_terms": ["cassandra", "fraud"],
                }
            ],
        }
        with patch(
            "src.web.routes.api.rank_experiences_for_job_usecase",
            return_value=payload,
        ) as usecase:
            response = client.post(
                "/api/matching/rank-experiences",
                json={
                    "profile_id": "corpus",
                    "job": {
                        "title": "Fraud Platform Engineer",
                        "description": "Cassandra fraud bank",
                    },
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["rankings"][0]["company"] == "TransUnion"
        usecase.assert_called_once()

    def test_route_requires_job(self, client: TestClient) -> None:
        response = client.post("/api/matching/rank-experiences", json={})
        assert response.status_code == 422
