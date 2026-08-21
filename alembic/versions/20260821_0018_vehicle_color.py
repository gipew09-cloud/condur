"""vehicles.color — цвет машины, который владелец выбирает вручную

Зачем: на мониторинге в метке стоит рисунок машины, и он перекрашивается в
выбранный цвет. Автоподбор цвета владелец отверг ещё в августе — цвет всегда
задаётся руками, как в Ставтрэке («Категория + Цвет» → картинка).

NULL и 'black' равнозначны: чёрный — цвет исходного рисунка.

Revision ID: 0018_vehicle_color
Revises: 0017_telemetry_voltage
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_vehicle_color"
down_revision: Union[str, None] = "0017_telemetry_voltage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vehicles", sa.Column("color", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("vehicles", "color")
