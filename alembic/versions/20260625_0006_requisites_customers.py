"""реквизиты Исполнителя (owners) + таблица customers — для акта 101 РС

Revision ID: 0006_requisites_customers
Revises: 0005_salary_per_month
Create Date: 2026-06-25

Шапка акта 101 РС требует реквизиты сторон. Реквизиты Исполнителя кладём
на owners (один владелец = один ИП), Заказчиков выносим в отдельную таблицу
customers (их может быть несколько). Всё nullable: заполняется на /requisites.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_requisites_customers"
down_revision: Union[str, None] = "0005_salary_per_month"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Реквизиты Исполнителя на owners ---
    op.add_column("owners", sa.Column("executor_name", sa.String(length=500), nullable=True))
    op.add_column("owners", sa.Column("inn", sa.String(length=12), nullable=True))
    op.add_column("owners", sa.Column("ogrnip", sa.String(length=20), nullable=True))
    op.add_column("owners", sa.Column("legal_address", sa.Text(), nullable=True))
    op.add_column("owners", sa.Column("bank_name", sa.String(length=255), nullable=True))
    op.add_column("owners", sa.Column("bank_account", sa.String(length=34), nullable=True))
    op.add_column("owners", sa.Column("corr_account", sa.String(length=34), nullable=True))
    op.add_column("owners", sa.Column("bik", sa.String(length=9), nullable=True))
    op.add_column("owners", sa.Column("signer_name", sa.String(length=255), nullable=True))

    # --- Таблица заказчиков ---
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id", sa.Integer(),
            sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("kpp", sa.String(length=9), nullable=True),
        sa.Column("legal_address", sa.Text(), nullable=True),
        sa.Column("bank_name", sa.String(length=255), nullable=True),
        sa.Column("bank_account", sa.String(length=34), nullable=True),
        sa.Column("corr_account", sa.String(length=34), nullable=True),
        sa.Column("bik", sa.String(length=9), nullable=True),
        sa.Column("contract_number", sa.String(length=100), nullable=True),
        sa.Column("contract_date", sa.Date(), nullable=True),
        sa.Column("signer_name", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_customers_owner_id", "customers", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_customers_owner_id", table_name="customers")
    op.drop_table("customers")
    for col in (
        "signer_name", "bik", "corr_account", "bank_account", "bank_name",
        "legal_address", "ogrnip", "inn", "executor_name",
    ):
        op.drop_column("owners", col)
