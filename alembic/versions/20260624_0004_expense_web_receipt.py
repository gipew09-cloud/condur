"""expense web receipt — фото чека, загруженное владельцем на сайте (Правка 5)

Revision ID: 0004_expense_web_receipt
Revises: 0003_trip_documents
Create Date: 2026-06-24

Байты фото храним в Postgres (LargeBinary) — без S3.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_expense_web_receipt"
down_revision: Union[str, None] = "0003_trip_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("expenses", sa.Column("receipt_web_data", sa.LargeBinary(), nullable=True))
    op.add_column("expenses", sa.Column("receipt_web_type", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("expenses", "receipt_web_type")
    op.drop_column("expenses", "receipt_web_data")
