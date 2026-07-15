"""store the originating email link on tasks

Revision ID: 0006_task_source_permalink
Revises: 0005_universal_imap_accounts
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_task_source_permalink"
down_revision: str | None = "0005_universal_imap_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("source_permalink", sa.String(length=2000)))


def downgrade() -> None:
    op.drop_column("tasks", "source_permalink")
