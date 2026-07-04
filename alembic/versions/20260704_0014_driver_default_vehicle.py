"""водители: «обычная машина» для анти-миссклика при старте смены

Revision ID: 0014_driver_default_vehicle
Revises: 0013_admin_notifications
Create Date: 2026-07-04

Водители обычно закреплены за машинами, но при старте смены выбирают машину
из списка и иногда промахиваются. Если выбранная машина не совпадает
с «обычной» — бот переспрашивает водителя и уведомляет владельца.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_driver_default_vehicle"
down_revision: Union[str, None] = "0013_admin_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "drivers",
        sa.Column(
            "default_vehicle_id", sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("drivers", "default_vehicle_id")
