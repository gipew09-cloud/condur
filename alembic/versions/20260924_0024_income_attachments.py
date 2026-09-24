"""Фото и документы к поступлениям.

Revision ID: 0024_income_attachments
Revises: 0023_finance_ledger
Create Date: 2026-09-24

Владелец 24.09.2026: «почему в доходах нельзя прикрепить документ или фото».
Поступление (предоплата, оплата по акту) подтверждается платёжкой, актом,
выпиской — их нужно хранить рядом с суммой, как чеки у расходов.

Отдельная таблица, а не общая с расходами: у поступления своя запись
(`manual_entries`), и удаление поступления должно уносить его вложения.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_income_attachments"
down_revision: Union[str, None] = "0023_finance_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "income_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manual_entry_id", sa.Integer(),
                  sa.ForeignKey("manual_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_income_attachments_owner_id", "income_attachments", ["owner_id"])
    op.create_index("ix_income_attachments_manual_entry_id", "income_attachments", ["manual_entry_id"])


def downgrade() -> None:
    op.drop_index("ix_income_attachments_manual_entry_id", table_name="income_attachments")
    op.drop_index("ix_income_attachments_owner_id", table_name="income_attachments")
    op.drop_table("income_attachments")
