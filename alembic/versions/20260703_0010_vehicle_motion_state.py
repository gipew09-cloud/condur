"""статус движения машины и время начала статуса

Revision ID: 0010_vehicle_motion_state
Revises: 0009_vehicle_telemetry
Create Date: 2026-07-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_vehicle_motion_state"
down_revision: Union[str, None] = "0009_vehicle_telemetry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vehicle_states", sa.Column("motion_status", sa.String(length=20), nullable=True))
    op.add_column("vehicle_states", sa.Column("motion_since_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE vehicle_states
           SET motion_status = CASE
                 WHEN COALESCE(speed_kmh, 0) > 3 THEN 'moving'
                 WHEN ignition IS TRUE THEN 'idle_engine'
                 ELSE 'stopped'
               END,
               motion_since_at = COALESCE(last_seen_at, updated_at)
         WHERE motion_status IS NULL
        """
    )
    op.create_check_constraint(
        "ck_vehicle_state_motion_status",
        "vehicle_states",
        "motion_status IS NULL OR motion_status IN ('moving','idle_engine','stopped','unknown')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_vehicle_state_motion_status", "vehicle_states", type_="check")
    op.drop_column("vehicle_states", "motion_since_at")
    op.drop_column("vehicle_states", "motion_status")
