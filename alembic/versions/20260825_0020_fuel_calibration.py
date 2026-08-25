"""vehicles.fuel_calibration — тарировочная таблица бака

Датчик отдаёт своё внутреннее число (X), литры (Y) получаются по таблице:
кусочно-линейная зависимость, снятая заливкой известных порций. У Ставтрэка
она лежит в настройках датчика («Датчики» → Топливо → Дополнительные), там же
видны пары X/Y и коэффициенты a/b для каждого отрезка.

Храним ПАРЫ X→Y, а не a/b: пары — исходные данные замеров, коэффициенты из них
считаются однозначно, а обратно — нет. Формат: [[x, y], [x, y], …] по возрастанию x.

Revision ID: 0020_fuel_calibration
Revises: 0019_fuel_level
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_fuel_calibration"
down_revision: Union[str, None] = "0019_fuel_level"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vehicles",
        sa.Column("fuel_calibration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("vehicles", sa.Column("tank_litres", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("vehicles", "tank_litres")
    op.drop_column("vehicles", "fuel_calibration")
