"""store per-account Outlook Graph delta cursor

Revision ID: 0004_outlook_delta_cursor
Revises: 0003_automatic_mail_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_outlook_delta_cursor"
down_revision: str | None = "0003_automatic_mail_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mail_accounts", sa.Column("outlook_delta_link", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mail_accounts", "outlook_delta_link")
