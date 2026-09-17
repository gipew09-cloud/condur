"""Фото из приложения водителя (одометр, ТТН, чек) — в базе

Railway стирает диск при каждой выкладке, поэтому фото лежат в Postgres, как
уже лежат документы рейсов и чеки с сайта (PROBLEMS №7). `source` — камера
или галерея (владелец 17.09.2026).

Revision ID: 0022_driver_photos
Revises: 0021_driver_app_access
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_driver_photos"
down_revision: Union[str, None] = "0021_driver_app_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "driver_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("driver_id", "client_id", name="uq_driver_photo_client"),
    )
    op.create_index("ix_driver_photos_owner_id", "driver_photos", ["owner_id"])
    op.create_index("ix_driver_photos_driver_id", "driver_photos", ["driver_id"])
    op.create_index("ix_driver_photos_sha256", "driver_photos", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_driver_photos_sha256", table_name="driver_photos")
    op.drop_index("ix_driver_photos_driver_id", table_name="driver_photos")
    op.drop_index("ix_driver_photos_owner_id", table_name="driver_photos")
    op.drop_table("driver_photos")
