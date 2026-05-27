"""Phase 14.6: smoke tests for the task kind catalog.

We run each task in Celery's ``eager`` mode (no broker, no worker
process) and verify:

* the task is registered with the right name,
* the task lands on the right queue per the 14.1 router,
* the payload schema rejects malformed input as a terminal failure
  (TypeError -- Celery's retry layer treats it as non-retryable),
* the happy path returns a structured stub result.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.tasks import tasks as task_kinds
from src.tasks.app import _task_router, celery_app


@pytest.fixture(autouse=True)
def _eager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)


# ---- Registration + routing ------------------------------------------


@pytest.mark.parametrize("name", task_kinds.KNOWN_TASK_NAMES)
def test_task_registered_in_celery_app(name: str) -> None:
    assert name in celery_app.tasks, f"task {name} not registered"


@pytest.mark.parametrize(
    ("task_name", "queue"),
    [
        ("search.refresh", "search"),
        ("search.daily_fanout", "search"),
        ("jobs.enrich", "maintenance"),
        ("materials.generate", "materials"),
        ("application.prepare", "application"),
        ("application.fill", "application"),
        ("application.submit", "application"),
        ("orchestration.plan_run", "search"),
        ("notifications.morning_digest", "maintenance"),
        ("maintenance.status_sync", "maintenance"),
        ("maintenance.cache_eviction", "maintenance"),
    ],
)
def test_router_assigns_each_task_kind_to_named_queue(
    task_name: str, queue: str
) -> None:
    assert _task_router(task_name) == {"queue": queue}


# ---- Payload validation ---------------------------------------------


def test_search_refresh_rejects_empty_payload() -> None:
    # Phase 19.3b: payload now requires either profile_id or query_id;
    # an empty dict raises TypeError so Celery treats it as terminal.
    with pytest.raises((TypeError, ValidationError)):
        task_kinds.search_refresh.apply(kwargs={}).get()


def test_search_refresh_invalid_query_id() -> None:
    """A malformed UUID is a terminal envelope, not an exception."""
    out = task_kinds.search_refresh.apply(
        kwargs={"query_id": "not-a-uuid", "source": "greenhouse"}
    ).get()
    assert out["task"] == "search.refresh"
    assert out["status"] == "invalid_query_id"


def test_search_refresh_profile_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.application.search_profiles.load_search_profiles_data",
        lambda: {"profiles": []},
    )
    out = task_kinds.search_refresh.apply(
        kwargs={"profile_id": "missing"}
    ).get()
    assert out["status"] == "profile_not_found"


def test_materials_generate_defaults_to_resume_and_cover_letter() -> None:
    out = task_kinds.materials_generate.apply(
        kwargs={"job_id": "job-1"}
    ).get()
    assert out["document_types"] == ["resume", "cover_letter"]


def test_materials_generate_rejects_missing_job_id() -> None:
    with pytest.raises((TypeError, ValidationError)):
        task_kinds.materials_generate.apply(kwargs={}).get()


def test_application_submit_round_trip() -> None:
    out = task_kinds.application_submit.apply(
        kwargs={"application_id": "app-99"}
    ).get()
    assert out["application_id"] == "app-99"


# ---- Beat-driven stubs --------------------------------------------------


def test_search_daily_fanout_enqueues_per_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 19.3b: ``search.daily_fanout`` enumerates saved-search
    profiles and enqueues one ``search.refresh`` child per profile.
    The no-op stub it used to be is gone."""
    monkeypatch.setattr(
        "src.application.search_profiles.load_search_profiles_data",
        lambda: {
            "profiles": [
                {"id": "SDE", "source": "linkedin", "max_pages": 5},
                {"id": "PM", "source": "ats", "max_pages": 10},
            ]
        },
    )

    sent: list[dict[str, Any]] = []

    class _AsyncResult:
        id = "fake-task-id"

    def fake_send_task(name, *, kwargs, queue=None):
        sent.append({"name": name, "kwargs": kwargs, "queue": queue})
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)

    out = task_kinds.search_daily_fanout.apply().get()
    assert out["status"] == "ok"
    assert out["profile_count"] == 2
    assert len(out["enqueued"]) == 2
    assert {row["name"] for row in sent} == {"search.refresh"}
    assert {row["kwargs"]["profile_id"] for row in sent} == {"SDE", "PM"}


def test_cache_eviction_runs_cleanup_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 18.4: ``maintenance.cache_eviction`` is no longer a stub.

    It opens a DB session and drives :func:`src.maintenance.artifacts.clean`
    plus :func:`purge_quarantine`. Here we stub out the engine + cleanup
    primitives so the eager-mode worker exercises the wiring and
    nothing else.
    """
    from contextlib import contextmanager

    calls: dict[str, int] = {"clean": 0, "purge": 0}

    @contextmanager
    def fake_session():
        class _Session:
            def begin(self):
                @contextmanager
                def _ctx():
                    yield
                return _ctx()
        yield _Session()

    monkeypatch.setattr(
        "src.core.database.get_session_factory",
        lambda *args, **kwargs: fake_session,
    )

    class _Report:
        def to_summary(self):
            return {"ok": True}

    def fake_clean(*_args, **_kwargs):
        calls["clean"] += 1
        return _Report()

    def fake_purge(*_args, **_kwargs):
        calls["purge"] += 1
        return _Report()

    monkeypatch.setattr("src.maintenance.artifacts.clean", fake_clean)
    monkeypatch.setattr("src.maintenance.artifacts.purge_quarantine", fake_purge)

    out = task_kinds.cache_eviction.apply().get()
    assert out["status"] == "ok"
    assert out["task"] == "maintenance.cache_eviction"
    assert calls == {"clean": 1, "purge": 1}
    assert "clean" in out["summaries"]
    assert "purge_quarantine" in out["summaries"]


def test_status_sync_accepts_optional_application_id() -> None:
    """No payload at all is valid (sweeps all apps); explicit id
    targets one."""
    out_a = task_kinds.status_sync.apply(kwargs={}).get()
    out_b = task_kinds.status_sync.apply(kwargs={"application_id": "x"}).get()
    assert out_a["task"] == "maintenance.status_sync"
    assert out_b["task"] == "maintenance.status_sync"
