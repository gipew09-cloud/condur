"""Книга расходов: одна таблица для всех трат, вложения, свои виды.

Revision ID: 0023_finance_ledger
Revises: 0022_driver_photos
Create Date: 2026-09-23

Владелец 23.09.2026: «расходы в финансах, в рейсах, в сменах — всё
запутано». Причина: трата записывалась в двух местах. Теперь расход — одна
запись в `expenses`, а рейс, смена и машина показывают её же.

Что меняется:
* водитель у расхода необязателен — трату может внести владелец;
* у расхода появляется машина, способ оплаты, поставщик, момент траты,
  кто внёс и номер «документа» (несколько трат одним вводом);
* категория больше не ограничена шестью кодами — владелец заводит свои;
* новые таблицы: вложения (фото и файлы) и свои виды расходов.

⚠️ Всё обратимо: downgrade возвращает как было. Но если к тому моменту
появились расходы без водителя, downgrade упадёт на NOT NULL — это честно:
такие записи нельзя молча выбросить.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_finance_ledger"
down_revision: Union[str, None] = "0022_driver_photos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_expense_category", "expenses", type_="check")
    op.alter_column("expenses", "category", type_=sa.String(60), existing_type=sa.String(20))
    op.alter_column("expenses", "driver_id", nullable=True, existing_type=sa.Integer())

    op.add_column("expenses", sa.Column("vehicle_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_expenses_vehicle_id", "expenses", "vehicles", ["vehicle_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_expenses_vehicle_id", "expenses", ["vehicle_id"])
    op.add_column("expenses", sa.Column("payment_method", sa.String(20), nullable=True))
    op.add_column("expenses", sa.Column("supplier", sa.String(255), nullable=True))
    op.add_column("expenses", sa.Column("spent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("expenses", sa.Column("created_by", sa.String(20), nullable=True))
    op.add_column("expenses", sa.Column("batch_id", sa.String(36), nullable=True))
    op.create_index("ix_expenses_batch_id", "expenses", ["batch_id"])

    op.create_table(
        "expense_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expense_id", sa.Integer(), sa.ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_expense_attachments_owner_id", "expense_attachments", ["owner_id"])
    op.create_index("ix_expense_attachments_expense_id", "expense_attachments", ["expense_id"])

    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_expense_categories_owner_name"),
    )
    op.create_index("ix_expense_categories_owner_id", "expense_categories", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_expense_categories_owner_id", table_name="expense_categories")
    op.drop_table("expense_categories")
    op.drop_index("ix_expense_attachments_expense_id", table_name="expense_attachments")
    op.drop_index("ix_expense_attachments_owner_id", table_name="expense_attachments")
    op.drop_table("expense_attachments")
    op.drop_index("ix_expenses_batch_id", table_name="expenses")
    op.drop_column("expenses", "batch_id")
    op.drop_column("expenses", "created_by")
    op.drop_column("expenses", "spent_at")
    op.drop_column("expenses", "supplier")
    op.drop_column("expenses", "payment_method")
    op.drop_index("ix_expenses_vehicle_id", table_name="expenses")
    op.drop_constraint("fk_expenses_vehicle_id", "expenses", type_="foreignkey")
    op.drop_column("expenses", "vehicle_id")
    op.alter_column("expenses", "driver_id", nullable=False, existing_type=sa.Integer())
    op.alter_column("expenses", "category", type_=sa.String(20), existing_type=sa.String(60))
    op.create_check_constraint(
        "ck_expense_category", "expenses",
        "category IN ('fuel','repair','parking','fine','toll','other')",
    )
