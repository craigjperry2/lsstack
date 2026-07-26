"""Create users, tasks, and generic outbox.

Revision ID: 20260725_0001
Revises:
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260725_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "session_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "session_version >= 0",
            name=op.f("ck_users_session_version_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email_normalized", name=op.f("uq_users_email_normalized")),
        schema="app",
    )
    op.create_index(
        op.f("ix_users_email_normalized"),
        "users",
        ["email_normalized"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=5000), nullable=True),
        sa.Column(
            "is_completed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "background_processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "description IS NULL OR length(description) <= 5000",
            name=op.f("ck_tasks_description_length"),
        ),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 200",
            name=op.f("ck_tasks_title_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.id"],
            name=op.f("fk_tasks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        schema="app",
    )
    op.create_index(
        op.f("ix_tasks_user_id"),
        "tasks",
        ["user_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_tasks_user_created",
        "tasks",
        ["user_id", "created_at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_outbox_messages_attempt_count_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
        schema="app",
    )
    op.create_index(
        "ix_outbox_unpublished_lease",
        "outbox_messages",
        ["published_at", "lease_expires_at"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_unpublished_lease",
        table_name="outbox_messages",
        schema="app",
    )
    op.drop_table("outbox_messages", schema="app")
    op.drop_index("ix_tasks_user_created", table_name="tasks", schema="app")
    op.drop_index(op.f("ix_tasks_user_id"), table_name="tasks", schema="app")
    op.drop_table("tasks", schema="app")
    op.drop_index(
        op.f("ix_users_email_normalized"),
        table_name="users",
        schema="app",
    )
    op.drop_table("users", schema="app")
