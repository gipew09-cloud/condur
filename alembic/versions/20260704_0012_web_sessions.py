"""веб-сессии кабинета: постоянный вход + управление устройствами

Revision ID: 0012_web_sessions
Revises: 0011_trip_pending_revenue
Create Date: 2026-07-04

Вместо 7-дневного JWT — серверные сессии: вход живёт, пока его не завершат
(«Выйти» на устройстве или владелец на «Реквизиты → Устройства»). В cookie —
случайный токен, в БД — только его SHA-256.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_web_sessions"
down_revision: Union[str, None] = "0011_trip_pending_revenue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id", sa.Integer(),
            sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_label", sa.String(length=120), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_web_sessions_owner_id", "web_sessions", ["owner_id"])
    op.create_index("ix_web_sessions_token_hash", "web_sessions", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_web_sessions_token_hash", table_name="web_sessions")
    op.drop_index("ix_web_sessions_owner_id", table_name="web_sessions")
    op.drop_table("web_sessions")
