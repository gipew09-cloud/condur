"""Уровень топлива и его температура с датчика ДУТ

Датчик поставили 25.08.2026 на Т557ОС178. В потоке Ставтрэка он приходит
параметрами fuel2 (проводной канал 2) и fuelTemp2. Значение СЫРОЕ — единицы
самого датчика, не литры: чтобы получить литры, нужна тарировочная таблица
бака от монтажника (пока её нет).

Храним сырое значение с первого дня: без него история не накопится, а
пересчитать задним числом можно будет в любой момент, когда таблица появится.

Revision ID: 0019_fuel_level
Revises: 0018_vehicle_color
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_fuel_level"
down_revision: Union[str, None] = "0018_vehicle_color"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("vehicle_telemetry_points", "vehicle_states"):
        op.add_column(table, sa.Column("fuel_level_raw", sa.Numeric(10, 2), nullable=True))
        op.add_column(table, sa.Column("fuel_temp_c", sa.Numeric(5, 1), nullable=True))


def downgrade() -> None:
    for table in ("vehicle_telemetry_points", "vehicle_states"):
        op.drop_column(table, "fuel_temp_c")
        op.drop_column(table, "fuel_level_raw")
