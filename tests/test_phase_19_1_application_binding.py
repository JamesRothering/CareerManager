"""Regression tests for the canonical Phase 19 application binding."""

from src.core.models import Application, ReviewQueueEntry


def test_application_uses_explicit_job_posting_binding() -> None:
    table = Application.__table__

    assert table.c.job_id.nullable is True
    assert table.c.job_posting_id.nullable is True
    targets = {fk.target_fullname for fk in table.c.job_posting_id.foreign_keys}
    assert targets == {"job_postings.id"}


def test_application_submit_policy_defaults_to_manual() -> None:
    column = Application.__table__.c.submit_policy

    assert column.nullable is False
    assert column.default.arg == "manual"
    assert str(column.server_default.arg) == "'manual'"


def test_review_queue_binds_internal_application_id() -> None:
    column = ReviewQueueEntry.__table__.c.application_id

    assert column.nullable is True
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert targets == {"applications.id"}
