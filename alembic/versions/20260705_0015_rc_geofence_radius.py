"""РЦ: индивидуальный радиус геозоны

Revision ID: 0015_rc_geofence_radius
Revises: 0014_driver_default_vehicle
Create Date: 2026-07-05

Склады разные: одни РЦ стоят в 200 м друг от друга, другие тянутся на
400+ м, и машина может встать с дальней стороны. NULL = глобальный
радиус по умолчанию (400 м, см. scheduler_jobs.RC_GEOFENCE_RADIUS_M).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_rc_geofence_radius"
down_revision: Union[str, None] = "0014_driver_default_vehicle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "distribution_centers",
        sa.Column("geofence_radius_m", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("distribution_centers", "geofence_radius_m")
