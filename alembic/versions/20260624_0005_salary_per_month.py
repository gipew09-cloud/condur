"""salary type fixed_per_month — помесячный оклад водителя

Revision ID: 0005_salary_per_month
Revises: 0004_expense_web_receipt
Create Date: 2026-06-24

Расширяем CHECK-констрейнт salary_type значением 'fixed_per_month'.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_salary_per_month"
down_revision: Union[str, None] = "0004_expense_web_receipt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_driver_salary_type", "drivers", type_="check")
    op.create_check_constraint(
        "ck_driver_salary_type",
        "drivers",
        "salary_type IN ('per_km','per_trip','percent','fixed_per_shift','fixed_per_month')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_driver_salary_type", "drivers", type_="check")
    op.create_check_constraint(
        "ck_driver_salary_type",
        "drivers",
        "salary_type IN ('per_km','per_trip','percent','fixed_per_shift')",
    )
