"""Phase 19.1: snapshot-level tags + per-snapshot/profile/scorer score cache.

The Phase 13 design treated the "result set" as the cache unit. Phase 19
flips the model: every search hits upstream, but the *analysis* of each
snapshot is cached once per ``(snapshot, profile_version, scorer_version)``.
This migration adds the storage for both halves of that split (D029):

* ``job_snapshots`` gains A1 objective tags --- ``tags`` (JSONB),
  ``tagger_version`` (INT), ``tags_status`` (pending/computing/ready/failed),
  ``tags_computed_at``. ``job_postings`` mirrors the latest snapshot's tags
  in ``latest_tags`` so the JobsView listing stays cheap.
* New ``job_posting_scores`` table holds A2 score-cache rows. The unique
  key includes profile and scorer version so a profile edit or scorer
  upgrade naturally cold-starts cache reads; old rows stay queryable for
  audit. ``tenant_id`` is required from day one (D026 pattern).

Revision ID: d4e2a7c19f08
Revises: f4e8c1d2a907
Create Date: 2026-05-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e2a7c19f08"
down_revision: str | Sequence[str] | None = "c3a7e1f2b048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TENANT_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    # --- A1: snapshot tags + posting denorm ---------------------------
    op.add_column(
        "job_snapshots",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "job_snapshots",
        sa.Column(
            "tagger_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "job_snapshots",
        sa.Column(
            "tags_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "job_snapshots",
        sa.Column("tags_computed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_job_snapshots_tagger_version",
        "job_snapshots",
        ["tenant_id", "tagger_version"],
    )
    op.create_index(
        "ix_job_snapshots_tags_status",
        "job_snapshots",
        ["tenant_id", "tags_status"],
    )

    op.add_column(
        "job_postings",
        sa.Column(
            "latest_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # --- A2: job_posting_scores --------------------------------------
    op.create_table(
        "job_posting_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id", sa.String(length=64), nullable=False, server_default=_TENANT_DEFAULT
        ),
        sa.Column(
            "posting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "job_postings.id",
                name="fk_job_posting_scores_posting",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "job_snapshots.id",
                name="fk_job_posting_scores_snapshot",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("profile_id", sa.String(length=200), nullable=False),
        sa.Column("profile_version", sa.String(length=64), nullable=False),
        sa.Column("scorer_version", sa.String(length=32), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=True),
        sa.Column("model_id", sa.String(length=120), nullable=True),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column(
            "score_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "snapshot_id",
            "profile_id",
            "profile_version",
            "scorer_version",
            name="uq_job_posting_scores_cache_key",
        ),
    )
    op.create_index(
        "ix_job_posting_scores_lookup",
        "job_posting_scores",
        [
            "tenant_id",
            "snapshot_id",
            "profile_id",
            "profile_version",
            "scorer_version",
        ],
    )
    op.create_index(
        "ix_job_posting_scores_profile_computed",
        "job_posting_scores",
        ["tenant_id", "profile_id", "computed_at"],
    )
    op.create_index(
        "ix_job_posting_scores_verdict_computed",
        "job_posting_scores",
        ["tenant_id", "verdict", "computed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_job_posting_scores_verdict_computed", table_name="job_posting_scores"
    )
    op.drop_index(
        "ix_job_posting_scores_profile_computed", table_name="job_posting_scores"
    )
    op.drop_index("ix_job_posting_scores_lookup", table_name="job_posting_scores")
    op.drop_table("job_posting_scores")

    op.drop_column("job_postings", "latest_tags")

    op.drop_index("ix_job_snapshots_tags_status", table_name="job_snapshots")
    op.drop_index("ix_job_snapshots_tagger_version", table_name="job_snapshots")
    op.drop_column("job_snapshots", "tags_computed_at")
    op.drop_column("job_snapshots", "tags_status")
    op.drop_column("job_snapshots", "tagger_version")
    op.drop_column("job_snapshots", "tags")
