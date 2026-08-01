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
    # Canonical applications created after this migration intentionally have
    # ``job_id = NULL``. Reconstruct the legacy binding before restoring the
    # old NOT NULL contract so downgrade remains data-preserving.
    op.execute(
        """
        UPDATE applications AS a
        SET job_id = legacy.id
        FROM job_postings AS p
        JOIN LATERAL (
            SELECT j.id
            FROM jobs AS j
            WHERE j.tenant_id = p.tenant_id
              AND j.source = p.source
              AND j.source_id = p.source_id
            ORDER BY j.discovered_at DESC NULLS LAST, j.id
            LIMIT 1
        ) AS legacy ON TRUE
        WHERE a.job_id IS NULL
          AND a.job_posting_id = p.id
        """
    )
    op.execute(
        """
        INSERT INTO jobs (
            id, tenant_id, source, source_id, company, title, location,
            employment_type, seniority, description, requirements, ats_type,
            application_url, raw_data, discovered_at
        )
        SELECT DISTINCT ON (p.id)
            p.id,
            p.tenant_id,
            p.source,
            p.source_id,
            p.company,
            COALESCE(s.title, 'Unavailable job'),
            s.location,
            s.employment_type,
            s.seniority,
            s.description,
            s.requirements,
            p.source,
            COALESCE(s.application_url, p.canonical_url),
            s.raw_data,
            COALESCE(s.scraped_at, p.first_seen_at, CURRENT_TIMESTAMP)
        FROM applications AS a
        JOIN job_postings AS p ON p.id = a.job_posting_id
        LEFT JOIN job_snapshots AS s
          ON s.id = COALESCE(a.job_snapshot_id, p.latest_snapshot_id)
        WHERE a.job_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM jobs AS j WHERE j.id = p.id)
        ORDER BY p.id, s.scraped_at DESC NULLS LAST
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE applications AS a
        SET job_id = p.id
        FROM job_postings AS p
        JOIN jobs AS j
          ON j.id = p.id
         AND j.tenant_id = p.tenant_id
         AND j.source = p.source
         AND j.source_id = p.source_id
        WHERE a.job_id IS NULL
          AND a.job_posting_id = p.id
        """
    )
    # A posting can be deleted through the new SET NULL FK. Preserve those
    # orphaned Application rows with an explicit legacy placeholder.
    op.execute(
        """
        INSERT INTO jobs (
            id, tenant_id, source, source_id, company, title, ats_type,
            raw_data, discovered_at
        )
        SELECT
            a.id,
            a.tenant_id,
            'downgrade_orphan',
            a.id::text,
            'Unknown company',
            'Unavailable job',
            'unknown',
            jsonb_build_object('_downgrade_orphan', true, 'application_id', a.id::text),
            COALESCE(a.created_at, CURRENT_TIMESTAMP)
        FROM applications AS a
        WHERE a.job_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM jobs AS j WHERE j.id = a.id)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE applications AS a
        SET job_id = j.id
        FROM jobs AS j
        WHERE a.job_id IS NULL
          AND j.id = a.id
          AND j.tenant_id = a.tenant_id
          AND j.source = 'downgrade_orphan'
          AND j.source_id = a.id::text
        """
    )
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
