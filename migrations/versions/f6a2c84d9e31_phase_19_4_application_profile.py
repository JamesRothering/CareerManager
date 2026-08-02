"""Phase 19.4: persist the profile used by application.fill.

Revision ID: f6a2c84d9e31
Revises: e5f1a92c7b40
Create Date: 2026-07-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a2c84d9e31"
down_revision: str | Sequence[str] | None = "e5f1a92c7b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column(
            "profile_id",
            sa.String(length=100),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("applications", "profile_id")
