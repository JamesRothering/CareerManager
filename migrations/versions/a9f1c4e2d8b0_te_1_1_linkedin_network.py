"""TE-1.1: LinkedIn official-export network table.

Stores connections and followers from LinkedIn's "Get a copy of your data"
CSVs. Playwright job-search scrape is unchanged. Upsert key is
``(tenant_id, kind, identity_key)`` — profile URL, or email if URL is absent.

Revision ID: a9f1c4e2d8b0
Revises: c3a7e1f2b048
Create Date: 2026-08-17 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9f1c4e2d8b0"
down_revision: str | Sequence[str] | None = "c3a7e1f2b048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "linkedin_network",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("identity_key", sa.String(length=400), nullable=False),
        sa.Column("profile_url", sa.Text(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("first_name", sa.String(length=200), nullable=True),
        sa.Column("last_name", sa.String(length=200), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("position", sa.String(length=300), nullable=True),
        sa.Column("headline", sa.String(length=400), nullable=True),
        sa.Column("connected_on", sa.Date(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            name="ck_linkedin_network_kind",
        ),
        sa.CheckConstraint(
            "("
            "(profile_url IS NOT NULL AND btrim(profile_url) <> '')"
            " OR (email IS NOT NULL AND btrim(email) <> '')"
            ")",
            name="ck_linkedin_network_url_or_email",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "identity_key",
            name="uq_linkedin_network_tenant_kind_identity",
        ),
    )
    op.create_index(
        "ix_linkedin_network_tenant_kind",
        "linkedin_network",
        ["tenant_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_linkedin_network_tenant_kind", table_name="linkedin_network")
    op.drop_table("linkedin_network")
