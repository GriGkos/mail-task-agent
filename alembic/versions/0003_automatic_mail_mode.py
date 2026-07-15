"""automatic mail mode and Gmail history cursor

Revision ID: 0003_automatic_mail_mode
Revises: 0002_multi_user_accounts
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_automatic_mail_mode"
down_revision: str | None = "0002_multi_user_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mail_accounts", sa.Column("gmail_history_id", sa.String(length=255)))
    op.add_column(
        "user_settings",
        sa.Column("gmail_mode", sa.String(length=30), nullable=False, server_default="automatic"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "gmail_mode")
    op.drop_column("mail_accounts", "gmail_history_id")
