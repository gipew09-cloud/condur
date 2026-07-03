"""черновая выручка от водителя до подтверждения владельцем

Revision ID: 0011_trip_pending_revenue
Revises: 0010_vehicle_motion_state
Create Date: 2026-07-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_trip_pending_revenue"
down_revision: Union[str, None] = "0010_vehicle_motion_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("driver_revenue_pending_rub", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("trips", "driver_revenue_pending_rub")
