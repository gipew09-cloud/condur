"""manual flags on shifts and trips (Блок D)

Revision ID: 0002_manual_flags
Revises: 0001_initial
Create Date: 2026-06-23

Добавляет признак «добавлено вручную» (оффлайн, задним числом) на смены и рейсы.
У таких записей нет одометра/GPS — пробег неизвестен.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_manual_flags"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shifts",
        sa.Column("is_manual", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "trips",
        sa.Column("is_manual", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("trips", "is_manual")
    op.drop_column("shifts", "is_manual")
