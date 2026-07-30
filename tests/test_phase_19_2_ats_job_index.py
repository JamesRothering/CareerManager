"""ATS discovery must enter the canonical Job Index/Snapshot flow."""

from unittest.mock import AsyncMock, patch

import pytest

from src.application.jobs import _ats_job_index_params, search_jobs
from src.intake.schema import RawJob


@pytest.mark.asyncio
async def test_ats_product_search_routes_through_job_index() -> None:
    job = RawJob(
        source="greenhouse",
        source_id="gh-123",
        company="Example",
        title="Software Engineer",
        application_url="https://boards.greenhouse.io/example/jobs/gh-123",
        ats_type="greenhouse",
    )
    index_event = {
        "source": "ats",
        "ok": True,
        "cached": False,
        "counts": {"scraped": 1},
    }

    with patch(
        "src.application.jobs._search_ats_with_job_index",
        new=AsyncMock(return_value=([job], index_event)),
    ) as indexed_search:
        result = await search_jobs(
            source="ats",
            use_job_index=True,
            profile=None,
            include_views=True,
        )

    indexed_search.assert_awaited_once()
    assert result["counts"]["ats"] == 1
    assert result["jobs"][0]["source_id"] == "gh-123"
    assert result["job_index"]["events"] == [index_event]


def test_ats_index_fingerprint_is_order_stable() -> None:
    left = _ats_job_index_params(
        {
            "profile": "default",
            "companies": {"lever": ["z", "a"], "greenhouse": ["b"]},
            "parse_jds": True,
            "use_llm": False,
        }
    )
    right = _ats_job_index_params(
        {
            "profile": "default",
            "companies": {"greenhouse": ["b"], "lever": ["a", "z"]},
            "parse_jds": True,
            "use_llm": False,
        }
    )

    assert left == right
    assert left["companies"]["lever"] == ["a", "z"]
