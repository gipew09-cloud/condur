"""Приложение водителя: выдача доступа, телефоны, действия с защитой от повторов

- driver_access_grants — одноразовые ссылка и код, в базе только отпечатки;
- driver_sessions — вход водителя с конкретного телефона (отдельно от кабинета);
- driver_actions — действия из приложения, уникальные по номеру с телефона.

Revision ID: 0021_driver_app_access
Revises: 0020_fuel_calibration
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_driver_app_access"
down_revision: Union[str, None] = "0020_fuel_calibration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "driver_access_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("issued_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_driver_access_grants_owner_id", "driver_access_grants", ["owner_id"])
    op.create_index("ix_driver_access_grants_driver_id", "driver_access_grants", ["driver_id"])

    op.create_table(
        "driver_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("grant_id", sa.Integer(), sa.ForeignKey("driver_access_grants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("device_label", sa.String(120), nullable=True),
        sa.Column("platform", sa.String(20), nullable=True),
        sa.Column("app_version", sa.String(20), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_driver_sessions_owner_id", "driver_sessions", ["owner_id"])
    op.create_index("ix_driver_sessions_driver_id", "driver_sessions", ["driver_id"])
    op.create_index("ix_driver_sessions_token_hash", "driver_sessions", ["token_hash"], unique=True)

    op.create_table(
        "driver_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("driver_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_id", sa.String(64), nullable=True),
        sa.Column("client_op_id", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("device_seq", sa.BigInteger(), nullable=True),
        sa.Column("client_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.UniqueConstraint("driver_id", "client_op_id", name="uq_driver_action_op"),
        sa.CheckConstraint("status IN ('accepted','rejected')", name="ck_driver_action_status"),
    )
    op.create_index("ix_driver_actions_owner_id", "driver_actions", ["owner_id"])
    op.create_index("ix_driver_actions_driver_id", "driver_actions", ["driver_id"])


def downgrade() -> None:
    op.drop_table("driver_actions")
    op.drop_table("driver_sessions")
    op.drop_table("driver_access_grants")
