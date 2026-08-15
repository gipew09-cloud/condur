"""
Расходы. Создаются водителем, одобряются владельцем.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
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

# Максимальное число необработанных (pending) расходов на одного водителя.
# Защищает от случайного спама кнопкой: водитель нажал трижды — третий раз
# блокируется, пока владелец не рассмотрит хотя бы один.
MAX_PENDING_PER_DRIVER = 10

# Cooldown между двумя расходами одной категории на одну и ту же сумму
# (защита от двойного нажатия при лаге сети): если за последние N секунд
# уже создан расход с такими же параметрами — считаем дублём и отклоняем.
DUPLICATE_WINDOW_SECONDS = 60


async def count_pending_expenses(session: AsyncSession, driver_id: int) -> int:
    """Сколько расходов этого водителя сейчас ждут решения владельца."""
    result = await session.execute(
        select(func.count(Expense.id)).where(
            Expense.driver_id == driver_id,
            Expense.status == "pending",
        )
    )
    return result.scalar_one() or 0


async def is_duplicate_expense(
    session: AsyncSession,
    *,
    driver_id: int,
    category: str,
    amount_rub: Decimal,
) -> bool:
    """True если за последний DUPLICATE_WINDOW_SECONDS уже создан расход
    с той же категорией и суммой — скорее всего двойное нажатие."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    result = await session.execute(
        select(func.count(Expense.id)).where(
            Expense.driver_id == driver_id,
            Expense.category == category,
            Expense.amount_rub == amount_rub,
            Expense.created_at >= cutoff,
        )
    )
    return (result.scalar_one() or 0) > 0


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
