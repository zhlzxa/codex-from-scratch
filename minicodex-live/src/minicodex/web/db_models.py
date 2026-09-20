"""PostgreSQL rows. Public API objects remain in accounts/sessions/store."""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AccountRow(Base):
    __tablename__ = "accounts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace_roots: Mapped[list[str]] = mapped_column(JSONB, default=list)
    limits: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)


class LoginSessionRow(Base):
    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_key: Mapped[str] = mapped_column(
        ForeignKey("accounts.key", ondelete="CASCADE"), index=True
    )
    created: Mapped[float] = mapped_column(Float, index=True)
    last_seen: Mapped[float] = mapped_column(Float, index=True)
    user_agent: Mapped[str] = mapped_column(String(200), default="")


class TenantRecord:
    account_key: Mapped[str] = mapped_column(
        ForeignKey("accounts.key", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)


class ThreadRow(TenantRecord, Base):
    __tablename__ = "threads"
    __table_args__ = (Index("ix_threads_owner_created", "account_key", "created"),)

    workspace: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ProviderRow(TenantRecord, Base):
    __tablename__ = "providers"
    __table_args__ = (
        Index("uq_provider_active", "account_key", unique=True, postgresql_where=text("active")),
    )

    name: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    api_key: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class McpRow(TenantRecord, Base):
    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Transport-specific settings keep their existing API shape.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)


class QuotaStartRow(Base):
    __tablename__ = "quota_starts"
    __table_args__ = (Index("ix_quota_owner_started", "account_key", "started"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_key: Mapped[str] = mapped_column(ForeignKey("accounts.key", ondelete="CASCADE"))
    started: Mapped[float] = mapped_column(Float)


class ImportRow(Base):
    __tablename__ = "legacy_imports"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    imported_at: Mapped[float] = mapped_column(Float)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB)
