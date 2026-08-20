"""US-8.2: persist keep / kill / later for LinkedIn network prune review.

Does not delete linkedin_network rows. Kill is an operator intent, not
a LinkedIn unfollow.

Revision ID: c8d2a1f4e9b7
Revises: b4e7c9a1d2f0
Create Date: 2026-08-20 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8d2a1f4e9b7"
down_revision: str | Sequence[str] | None = "b4e7c9a1d2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "linkedin_network_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("identity_key", sa.String(length=400), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
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
            "kind IN ('connection', 'follower')",
            name="ck_linkedin_network_decisions_kind",
        ),
        sa.CheckConstraint(
            "decision IN ('keep', 'kill', 'later')",
            name="ck_linkedin_network_decisions_decision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "identity_key",
            name="uq_linkedin_network_decisions_tenant_kind_identity",
        ),
    )
    op.create_index(
        "ix_linkedin_network_decisions_tenant_decision",
        "linkedin_network_decisions",
        ["tenant_id", "decision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_linkedin_network_decisions_tenant_decision",
        table_name="linkedin_network_decisions",
    )
    op.drop_table("linkedin_network_decisions")
