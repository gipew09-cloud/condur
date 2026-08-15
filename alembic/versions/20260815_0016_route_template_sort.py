"""route_templates.sort_order — свой порядок маршрутов для водителя

Зачем: водитель в боте видит маршруты в том же порядке, что владелец задал на
сайте. Раньше сортировка была по алфавиту, и часто используемый маршрут мог
оказаться в середине списка — водитель его не замечал и выбирал не тот склад.

Revision ID: 0016_route_template_sort
Revises: 0015_rc_geofence_radius
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_route_template_sort"
down_revision: Union[str, None] = "0015_rc_geofence_radius"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "route_templates",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    # Существующие маршруты нумеруем по текущему алфавитному порядку внутри
    # каждого склада — чтобы после обновления список выглядел как раньше,
    # а не перемешался.
    op.execute(
        """
        UPDATE route_templates rt
        SET sort_order = sub.rn
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY owner_id, origin ORDER BY destination
                   ) * 10 AS rn
            FROM route_templates
        ) AS sub
        WHERE rt.id = sub.id
        """
    )


def downgrade() -> None:
    op.drop_column("route_templates", "sort_order")
