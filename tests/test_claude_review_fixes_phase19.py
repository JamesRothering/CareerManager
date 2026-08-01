"""Regression coverage for confirmed Claude CLI review findings."""

from __future__ import annotations

import importlib
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from src.core.models import JobPosting, JobSnapshot
from src.orchestration import plan_run
from src.tracker.database import get_applications_with_jobs


def test_canonical_binding_downgrade_rebuilds_legacy_job_ids(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.e5f1a92c7b40_phase_19_1_canonical_application_binding"
    )
    calls = []

    class Operations:
        def execute(self, sql):
            calls.append(("execute", str(sql)))

        def drop_index(self, *args, **kwargs):
            calls.append(("drop_index", args, kwargs))

        def drop_constraint(self, *args, **kwargs):
            calls.append(("drop_constraint", args, kwargs))

        def drop_column(self, *args, **kwargs):
            calls.append(("drop_column", args, kwargs))

        def alter_column(self, *args, **kwargs):
            calls.append(("alter_column", args, kwargs))

    monkeypatch.setattr(migration, "op", Operations())

    migration.downgrade()

    executed_sql = "\n".join(call[1] for call in calls if call[0] == "execute")
    alter_index = next(i for i, call in enumerate(calls) if call[0] == "alter_column")
    execute_indexes = [i for i, call in enumerate(calls) if call[0] == "execute"]
    assert execute_indexes
    assert max(execute_indexes) < alter_index
    assert "INSERT INTO jobs" in executed_sql
    assert "downgrade_orphan" in executed_sql
    assert "SET job_id" in executed_sql


def test_plan_binding_failure_isolated_to_one_selected_row(monkeypatch):
    first_posting_id = uuid4()
    second_posting_id = uuid4()
    postings = {
        first_posting_id: SimpleNamespace(
            id=first_posting_id, tenant_id="default", latest_snapshot_id=None
        ),
        second_posting_id: SimpleNamespace(
            id=second_posting_id, tenant_id="default", latest_snapshot_id=None
        ),
    }

    class ScalarResult:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        def __init__(self):
            self.added = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def begin(self):
            return nullcontext()

        def begin_nested(self):
            return nullcontext()

        def get(self, model, identifier):
            if model is JobPosting:
                return postings.get(identifier)
            if model is JobSnapshot:
                return None
            return None

        def execute(self, _statement):
            return ScalarResult()

        def add(self, row):
            self.added.append(row)

        def flush(self):
            for row in self.added:
                if getattr(row, "id", None) is None:
                    row.id = uuid4()

    session = Session()
    monkeypatch.setattr(
        "src.core.database.get_session_factory", lambda: lambda: session
    )
    calls = {"count": 0}

    def create_entry(_session, _args):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated row-local insert race")
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr("src.application.review.create_entry", create_entry)
    selected = [
        SimpleNamespace(job_id=str(first_posting_id), job_snapshot_id=None),
        SimpleNamespace(job_id=str(second_posting_id), job_snapshot_id=None),
    ]

    bindings = plan_run._create_applications_and_review_entries(
        tenant_id="default",
        run_id="run-1",
        selected=selected,
        profile_id="default",
        submit_policy="manual",
    )

    assert calls["count"] == 2
    assert len(bindings) == 1
    assert bindings[0].job_posting_id == str(second_posting_id)


def test_orphaned_application_remains_visible_in_unified_read_model():
    application = SimpleNamespace(
        id=uuid4(),
        job_posting_id=None,
        job_id=None,
    )

    class Result:
        @staticmethod
        def all():
            return [(application, None, None, None)]

    class Session:
        @staticmethod
        def execute(_statement):
            return Result()

    rows = get_applications_with_jobs(Session(), limit=None)

    assert len(rows) == 1
    returned_application, job = rows[0]
    assert returned_application is application
    assert job.id == application.id
    assert job.company == "Unknown company"
    assert job.title == "Unavailable job"
    assert job.ats_type == "orphaned"
    assert job.raw_data["orphaned"] is True
