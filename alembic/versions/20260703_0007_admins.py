"""администраторы кабинета (доп. доступ кроме владельца)

Revision ID: 0007_admins
Revises: 0006_requisites_customers
Create Date: 2026-07-03

Владелец может дать полный доступ к своему кабинету другому человеку (админу)
по Telegram ID. Вход админа — тот же код-флоу, JWT выдаётся с owner_id владельца.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_admins"
down_revision: Union[str, None] = "0006_requisites_customers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id", sa.Integer(),
            sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_admins_owner_id", "admins", ["owner_id"])
    op.create_unique_constraint("uq_admins_telegram_id", "admins", ["telegram_id"])
    op.create_index("ix_admins_telegram_id", "admins", ["telegram_id"])


def downgrade() -> None:
    op.drop_index("ix_admins_telegram_id", table_name="admins")
    op.drop_constraint("uq_admins_telegram_id", "admins", type_="unique")
    op.drop_index("ix_admins_owner_id", table_name="admins")
    op.drop_table("admins")
