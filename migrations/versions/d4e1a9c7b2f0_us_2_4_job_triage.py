"""US-2.4: persist pursue / skip / later for discovered jobs.

Revision ID: d4e1a9c7b2f0
Revises: c8d2a1f4e9b7
Create Date: 2026-08-24 02:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e1a9c7b2f0"
down_revision: str | Sequence[str] | None = "c8d2a1f4e9b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "job_triage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("job_key", sa.String(length=400), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("job", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pursue', 'skip', 'later')",
            name="ck_job_triage_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "job_key", name="uq_job_triage_tenant_job_key"),
    )
    op.create_index(
        "ix_job_triage_tenant_status",
        "job_triage",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_triage_tenant_status", table_name="job_triage")
    op.drop_table("job_triage")
