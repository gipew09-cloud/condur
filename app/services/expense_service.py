"""
Расходы. Создаются водителем, одобряются владельцем.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense


VALID_CATEGORIES = ("fuel", "repair", "parking", "fine", "toll", "other")

CATEGORY_LABELS = {
    "fuel": "Топливо",
    "repair": "Ремонт",
    "parking": "Парковка",
    "fine": "Штраф",
    "toll": "Платная дорога",
    "other": "Прочее",
}


async def create_expense(
    session: AsyncSession,
    *,
    owner_id: int,
    driver_id: int,
    shift_id: int | None,
    trip_id: int | None,
    category: str,
    amount_rub: Decimal,
    receipt_photo_id: str | None,
    description: str | None = None,
) -> Expense:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Неизвестная категория расхода: {category}")
    expense = Expense(
        owner_id=owner_id,
        driver_id=driver_id,
        shift_id=shift_id,
        trip_id=trip_id,
        category=category,
        amount_rub=amount_rub,
        receipt_photo_url=receipt_photo_id,
        description=description,
        status="pending",
    )
    session.add(expense)
    return expense


async def decide_expense(
    session: AsyncSession, *, expense_id: int, approve: bool
) -> Expense | None:
    result = await session.execute(select(Expense).where(Expense.id == expense_id))
    expense = result.scalar_one_or_none()
    if expense is None:
        return None
    if expense.status != "pending":
        return expense  # уже решено — идемпотентность
    expense.status = "approved" if approve else "rejected"
    expense.decided_at = datetime.now(timezone.utc)
    return expense
