"""Posting ids and internal application ids are separate task domains."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.orchestration.plan_run import (
    ApplicationBinding,
    PlanRunError,
    normalize_submit_policy,
    run_plan,
)


def test_auto_submit_alias_maps_to_after_approval() -> None:
    assert normalize_submit_policy(submit_policy=None, auto_submit=True) == "after_approval"
    assert normalize_submit_policy(submit_policy=None, auto_submit=False) == "manual"
    assert (
        normalize_submit_policy(submit_policy="manual", auto_submit=True)
        == "manual"
    )
    with pytest.raises(PlanRunError):
        normalize_submit_policy(submit_policy="immediate", auto_submit=False)


def test_plan_enqueues_only_internal_application_ids() -> None:
    posting_id = str(uuid4())
    application_id = str(uuid4())
    captured_policy = None
    enqueued: list[tuple[str, dict]] = []

    async def search(**_kwargs):
        return {"jobs": [{"id": posting_id}]}

    def score(_jobs, _profile):
        return [
            SimpleNamespace(
                job_id=posting_id,
                job_snapshot_id=None,
                company="Acme",
                title="Engineer",
                final_score=0.9,
                disqualified=False,
            )
        ]

    def prepare(*, submit_policy, **_kwargs):
        nonlocal captured_policy
        captured_policy = submit_policy
        return [
            ApplicationBinding(
                job_posting_id=posting_id,
                application_id=application_id,
                review_entry_id=str(uuid4()),
            )
        ]

    def enqueue(name, payload):
        enqueued.append((name, payload))
        return str(uuid4())

    report = asyncio.run(
        run_plan(
            tenant_id="default",
            auto_submit=True,
            skip_previously_applied=False,
            search_fn=search,
            score_fn=score,
            enqueue_fn=enqueue,
            prepare_applications_fn=prepare,
            pause_root=Path("__phase19_nonexistent_pause_root__"),
        )
    )

    assert captured_policy == "after_approval"
    assert report.application_ids == [application_id]
    assert report.application_submit_task_ids == []
    materials = next(payload for name, payload in enqueued if name == "materials.generate")
    prepare_payload = next(
        payload for name, payload in enqueued if name == "application.prepare"
    )
    assert materials["job_id"] == posting_id
    assert materials["application_id"] == application_id
    assert prepare_payload == {"application_id": application_id}
    assert all(name != "application.submit" for name, _payload in enqueued)
