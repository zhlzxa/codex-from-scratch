"""Initial console schema. Keep this snapshot independent of runtime models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_console"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("admin", sa.Boolean(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("workspace_roots", JSONB(), nullable=False),
        sa.Column("limits", JSONB(), nullable=False),
    )
    op.create_table(
        "login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "account_key",
            sa.String(64),
            sa.ForeignKey("accounts.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("last_seen", sa.Float(), nullable=False),
        sa.Column("user_agent", sa.String(200), nullable=False),
    )
    for column in ("account_key", "created", "last_seen"):
        op.create_index(f"ix_login_sessions_{column}", "login_sessions", [column])
    op.create_table(
        "threads",
        *tenant_columns(),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("settings", JSONB(), nullable=False),
    )
    op.create_index("ix_threads_owner_created", "threads", ["account_key", "created"])
    op.create_table(
        "providers",
        *tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "uq_provider_active",
        "providers",
        ["account_key"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "mcp_servers",
        *tenant_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
    )
    op.create_table(
        "quota_starts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_key",
            sa.String(64),
            sa.ForeignKey("accounts.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started", sa.Float(), nullable=False),
    )
    op.create_index("ix_quota_owner_started", "quota_starts", ["account_key", "started"])
    op.create_table(
        "legacy_imports",
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("imported_at", sa.Float(), nullable=False),
        sa.Column("counts", JSONB(), nullable=False),
    )


def tenant_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "account_key",
            sa.String(64),
            sa.ForeignKey("accounts.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.String(64), primary_key=True),
    ]


def downgrade() -> None:
    for table in (
        "legacy_imports",
        "quota_starts",
        "mcp_servers",
        "providers",
        "threads",
        "login_sessions",
        "accounts",
    ):
        op.drop_table(table)
