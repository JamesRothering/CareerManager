"""End-to-end unit coverage for the persisted application.fill boundary."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from src.application.fill import run_application_fill
from src.core.models import Application, Job, JobPosting, JobSnapshot
from src.core.state_machine import AppStatus
from src.execution.ats.base import ApplicationResult


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def begin(self):
        return nullcontext()

    def get(self, model, identifier):
        return self.rows.get((model, identifier))


def test_fill_resolves_application_and_persists_review_result(monkeypatch) -> None:
    application_id = uuid4()
    posting_id = uuid4()
    snapshot_id = uuid4()
    resume_path = Path("README.md").resolve()
    application = SimpleNamespace(
        id=application_id,
        tenant_id="default",
        profile_id="default",
        job_id=None,
        job_posting_id=posting_id,
        job_snapshot_id=snapshot_id,
        status=AppStatus.MATERIALS_READY,
        resume_version=str(resume_path),
        cover_letter_version=None,
        qa_responses={"Authorized?": "Yes"},
        error_log=None,
        state_history=[
            {
                "timestamp": "2026-07-29T00:00:00+00:00",
                "event": "FILL_ENQUEUED",
                "from": "MATERIALS_READY",
                "to": "MATERIALS_READY",
                "meta": {"fill_task_id": "fill-1"},
            }
        ],
    )
    posting = SimpleNamespace(
        id=posting_id,
        tenant_id="default",
        source="greenhouse",
        source_id="gh-1",
        company="Acme",
        canonical_url="https://boards.greenhouse.io/acme/jobs/gh-1",
        latest_snapshot_id=snapshot_id,
        latest_tags={},
    )
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id="default",
        posting_id=posting_id,
        title="Engineer",
        location="Remote",
        employment_type="fulltime",
        seniority="entry",
        description="Build reliable systems",
        requirements={},
        application_url=posting.canonical_url,
        raw_data={},
        tags={},
        tags_status="ready",
    )
    rows = {
        (Application, application_id): application,
        (JobPosting, posting_id): posting,
        (JobSnapshot, snapshot_id): snapshot,
        (Job, None): None,
    }
    session = _Session(rows)
    monkeypatch.setattr(
        "src.core.database.get_session_factory",
        lambda *_args, **_kwargs: lambda: session,
    )
    monkeypatch.setattr(
        "src.application.jobs._load_profile",
        lambda _profile_id=None: {"identity": {"name": "Test User"}},
    )
    persisted = {}

    def sync(_session, app_id, state, payload):
        persisted.update(
            application_id=app_id,
            status=str(state.status),
            history=list(state.history),
            payload=payload,
        )
        application.status = str(state.status)
        return application

    monkeypatch.setattr("src.tracker.database.sync_state_to_db", sync)
    execution_args = {}

    async def executor(**kwargs):
        execution_args.update(kwargs)
        state = kwargs["state"]
        state.transition(AppStatus.FORM_OPENED)
        state.transition(AppStatus.FIELDS_MAPPED, fields_filled=2)
        state.transition(AppStatus.FILES_UPLOADED, files=["README.md"])
        state.transition(AppStatus.QUESTIONS_ANSWERED, qa_count=1)
        state.transition(AppStatus.REVIEW_REQUIRED)
        result = ApplicationResult(job_id=str(application_id))
        result.status = AppStatus.REVIEW_REQUIRED
        result.fields_filled = 2
        result.fields_total = 3
        result.fill_details = [{"label": "Email", "filled": True}]
        result.files_uploaded = ["README.md"]
        result.qa_answered = 1
        return result

    result = run_application_fill(
        application_id=str(application_id),
        tenant_id="default",
        headless=True,
        executor=executor,
    )

    assert result["status"] == "review_required"
    assert result["application_status"] == "REVIEW_REQUIRED"
    assert result["fill_details"] == [{"label": "Email", "filled": True}]
    assert execution_args["auto_submit"] is False
    assert execution_args["resume_path"] == resume_path
    assert persisted["application_id"] == application_id
    assert persisted["status"] == "REVIEW_REQUIRED"
    assert persisted["payload"]["fields_filled"] == 2
    assert persisted["history"][0]["event"] == "FILL_ENQUEUED"


def test_fill_rejects_non_application_id_without_database_access() -> None:
    result = run_application_fill(
        application_id="job-posting-not-an-application",
        tenant_id="default",
    )

    assert result["status"] == "invalid_id"

class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def test_prepare_enqueues_fill_once_with_internal_application_id(monkeypatch) -> None:
    from src.tasks import tasks as task_kinds

    application_id = uuid4()
    application = SimpleNamespace(
        id=application_id,
        tenant_id="default",
        resume_version=str(Path("README.md").resolve()),
        cover_letter_version=None,
        status="MATERIALS_READY",
        state_history=[],
    )
    entry = SimpleNamespace(
        id=uuid4(),
        tenant_id="default",
        application_id=application_id,
        status="pending",
        materials_path=None,
    )

    class PrepareSession(_Session):
        def execute(self, _statement):
            return _ScalarResult(entry)

    session = PrepareSession({(Application, application_id): application})
    monkeypatch.setattr(
        "src.core.database.get_session_factory",
        lambda *_args, **_kwargs: lambda: session,
    )
    queued = []

    def send_task(name, kwargs):
        queued.append((name, kwargs))
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr("src.tasks.app.celery_app.send_task", send_task)

    first = task_kinds.application_prepare.run(application_id=str(application_id))
    second = task_kinds.application_prepare.run(application_id=str(application_id))

    assert first["status"] == "fill_queued"
    assert second["status"] == "already_queued"
    assert queued == [
        ("application.fill", {"application_id": str(application_id)})
    ]
    assert any(event["event"] == "FILL_ENQUEUED" for event in application.state_history)
