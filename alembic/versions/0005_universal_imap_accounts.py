"""add universal IMAP account cursor and setup sessions

Revision ID: 0005_universal_imap_accounts
Revises: 0004_outlook_delta_cursor
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_universal_imap_accounts"
down_revision: str | None = "0004_outlook_delta_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mail_accounts", sa.Column("imap_uidvalidity", sa.String(length=255)))
    op.add_column("mail_accounts", sa.Column("imap_last_uid", sa.String(length=255)))
    op.create_table(
        "mail_setup_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mail_setup_sessions_user_id", "mail_setup_sessions", ["user_id"])
    op.create_index("ix_mail_setup_sessions_provider", "mail_setup_sessions", ["provider"])
    op.create_index("ix_mail_setup_sessions_expires_at", "mail_setup_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mail_setup_sessions_expires_at", table_name="mail_setup_sessions")
    op.drop_index("ix_mail_setup_sessions_provider", table_name="mail_setup_sessions")
    op.drop_index("ix_mail_setup_sessions_user_id", table_name="mail_setup_sessions")
    op.drop_table("mail_setup_sessions")
    op.drop_column("mail_accounts", "imap_last_uid")
    op.drop_column("mail_accounts", "imap_uidvalidity")
