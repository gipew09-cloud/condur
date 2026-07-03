"""справочник РЦ для канонических адресов в актах

Revision ID: 0008_distribution_centers
Revises: 0007_admins
Create Date: 2026-07-03

Храним названия, адреса, координаты и алиасы РЦ владельца. В акте сверки
пункт назначения можно нормализовать к официальному адресу из справочника.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_distribution_centers"
down_revision: Union[str, None] = "0007_admins"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "distribution_centers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id", sa.Integer(),
            sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_distribution_centers_owner_id", "distribution_centers", ["owner_id"])
    op.create_unique_constraint(
        "uq_distribution_centers_owner_name",
        "distribution_centers",
        ["owner_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_distribution_centers_owner_name",
        "distribution_centers",
        type_="unique",
    )
    op.drop_index("ix_distribution_centers_owner_id", table_name="distribution_centers")
    op.drop_table("distribution_centers")
