"""TE-1.1: store every file in LinkedIn's complete data archive.

``linkedin_network`` stays as the typed connections/followers projection.
The complete export (Complete_LinkedInDataExport_*.zip) also has Messages,
Profile, Positions, Invitations, Shares, and dozens of other CSVs plus
some JSON/TXT/HTML/media. These tables hold that whole zip.

Revision ID: b4e7c9a1d2f0
Revises: a9f1c4e2d8b0
Create Date: 2026-08-18 17:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4e7c9a1d2f0"
down_revision: str | Sequence[str] | None = "a9f1c4e2d8b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "linkedin_export_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "source_kind IN ('zip', 'directory')",
            name="ck_linkedin_export_runs_source_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_linkedin_export_runs_tenant",
        "linkedin_export_runs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_linkedin_export_runs_tenant_sha",
        "linkedin_export_runs",
        ["tenant_id", "source_sha256"],
    )

    op.create_table(
        "linkedin_export_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.String(length=20), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("header", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "media_kind IN ('csv', 'json', 'text', 'html', 'binary', 'other')",
            name="ck_linkedin_export_files_media_kind",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["linkedin_export_runs.id"],
            name="fk_linkedin_export_files_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "relative_path",
            name="uq_linkedin_export_files_run_path",
        ),
    )
    op.create_index(
        "ix_linkedin_export_files_run",
        "linkedin_export_files",
        ["run_id"],
    )
    op.create_index(
        "ix_linkedin_export_files_tenant_path",
        "linkedin_export_files",
        ["tenant_id", "relative_path"],
    )

    op.create_table(
        "linkedin_export_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default=_TENANT_DEFAULT,
        ),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["linkedin_export_files.id"],
            name="fk_linkedin_export_rows_file",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["linkedin_export_runs.id"],
            name="fk_linkedin_export_rows_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "file_id",
            "row_index",
            name="uq_linkedin_export_rows_file_index",
        ),
    )
    op.create_index(
        "ix_linkedin_export_rows_file",
        "linkedin_export_rows",
        ["file_id"],
    )
    op.create_index(
        "ix_linkedin_export_rows_run",
        "linkedin_export_rows",
        ["run_id"],
    )
    op.create_index(
        "ix_linkedin_export_rows_tenant_path",
        "linkedin_export_rows",
        ["tenant_id", "relative_path"],
    )


def downgrade() -> None:
    op.drop_index("ix_linkedin_export_rows_tenant_path", table_name="linkedin_export_rows")
    op.drop_index("ix_linkedin_export_rows_run", table_name="linkedin_export_rows")
    op.drop_index("ix_linkedin_export_rows_file", table_name="linkedin_export_rows")
    op.drop_table("linkedin_export_rows")
    op.drop_index("ix_linkedin_export_files_tenant_path", table_name="linkedin_export_files")
    op.drop_index("ix_linkedin_export_files_run", table_name="linkedin_export_files")
    op.drop_table("linkedin_export_files")
    op.drop_index("ix_linkedin_export_runs_tenant_sha", table_name="linkedin_export_runs")
    op.drop_index("ix_linkedin_export_runs_tenant", table_name="linkedin_export_runs")
    op.drop_table("linkedin_export_runs")
