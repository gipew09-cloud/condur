"""Напряжение бортсети и батарея трекера в телеметрии

Зачем: Ставтрэк уже присылает эти значения в каждом пакете
(`params={'power': 27.98, 'battery': 4.23, ...}`), но мы их только читали для
определения зажигания и выбрасывали. Теперь храним:

  * `voltage` — напряжение бортсети. Владелец видит его в карточке машины на
    мониторинге, как в Ставтрэке.
  * `battery_voltage` — собственная батарея трекера. Это ключ к проблеме №21
    из PROBLEMS: когда на машине выключают массу, `voltage` падает почти в ноль,
    а трекер ещё несколько часов живёт на своей батарее и шлёт точки. Без этой
    колонки «обесточена» от «стоит с заглушённым двигателем» не отличить.

Revision ID: 0017_telemetry_voltage
Revises: 0016_route_template_sort
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_telemetry_voltage"
down_revision: Union[str, None] = "0016_route_template_sort"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Numeric(6,2): до 9999.99 В с запасом — бортсеть 12/24 В, но у EGTS-пути
    # значение приходит в 0.1 В и теоретически может быть шумным.
    op.add_column(
        "vehicle_telemetry_points",
        sa.Column("voltage", sa.Numeric(6, 2), nullable=True),
    )
    op.add_column(
        "vehicle_telemetry_points",
        sa.Column("battery_voltage", sa.Numeric(5, 2), nullable=True),
    )
    op.add_column("vehicle_states", sa.Column("voltage", sa.Numeric(6, 2), nullable=True))
    op.add_column(
        "vehicle_states", sa.Column("battery_voltage", sa.Numeric(5, 2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("vehicle_states", "battery_voltage")
    op.drop_column("vehicle_states", "voltage")
    op.drop_column("vehicle_telemetry_points", "battery_voltage")
    op.drop_column("vehicle_telemetry_points", "voltage")
