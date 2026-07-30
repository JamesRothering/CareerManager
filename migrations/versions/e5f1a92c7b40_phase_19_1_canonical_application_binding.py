"""Phase 19.1: bind applications to the canonical Job Index model.

The original ``applications.job_id`` foreign key points at the legacy
``jobs`` intake table while Phase 13+ search, scoring, and review use
``job_postings`` and immutable ``job_snapshots``. Add an explicit
``job_posting_id`` binding and make the legacy binding optional. Review
queue rows also gain a direct ``application_id`` so task payloads never
need to reinterpret a posting id as an application id.

The old column/table are intentionally retained for one migration window.
Existing rows are backfilled where an unambiguous posting already exists;
all new writes use the canonical columns.

Revision ID: e5f1a92c7b40
Revises: d4e2a7c19f08
Create Date: 2026-07-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f1a92c7b40"
down_revision: str | Sequence[str] | None = "d4e2a7c19f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("applications", "job_id", existing_type=postgresql.UUID(), nullable=True)
    op.add_column(
        "applications",
        sa.Column("job_posting_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_applications_job_posting",
        "applications",
        "job_postings",
        ["job_posting_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_applications_job_posting_id",
        "applications",
        ["job_posting_id"],
    )
    op.add_column(
        "applications",
        sa.Column(
            "submit_policy",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
    )

    # Backfill only an unambiguous canonical match. Historical rows that do
    # not have a matching indexed posting keep their legacy binding and remain
    # readable through the compatibility adapter.
    op.execute(
        """
        UPDATE applications AS a
        SET job_posting_id = p.id
        FROM jobs AS j
        JOIN job_postings AS p
          ON p.tenant_id = j.tenant_id
         AND p.source = j.source
         AND p.source_id = j.source_id
        WHERE a.job_id = j.id
          AND a.job_posting_id IS NULL
        """
    )

    op.add_column(
        "review_queue",
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_review_queue_application",
        "review_queue",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_review_queue_application",
        "review_queue",
        ["application_id"],
    )
    op.execute(
        """
        UPDATE review_queue AS rq
        SET application_id = (
            SELECT a.id
            FROM applications AS a
            WHERE a.tenant_id = rq.tenant_id
              AND (
                    a.job_posting_id = rq.job_id
                 OR (a.job_posting_id IS NULL AND a.job_id = rq.job_id)
              )
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT 1
        )
        WHERE rq.application_id IS NULL
          AND EXISTS (
            SELECT 1
            FROM applications AS a
            WHERE a.tenant_id = rq.tenant_id
              AND (
                    a.job_posting_id = rq.job_id
                 OR (a.job_posting_id IS NULL AND a.job_id = rq.job_id)
              )
          )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_review_queue_application", table_name="review_queue")
    op.drop_constraint(
        "fk_review_queue_application",
        "review_queue",
        type_="foreignkey",
    )
    op.drop_column("review_queue", "application_id")

    op.drop_column("applications", "submit_policy")
    op.drop_index("ix_applications_job_posting_id", table_name="applications")
    op.drop_constraint(
        "fk_applications_job_posting",
        "applications",
        type_="foreignkey",
    )
    op.drop_column("applications", "job_posting_id")
    op.alter_column("applications", "job_id", existing_type=postgresql.UUID(), nullable=False)
