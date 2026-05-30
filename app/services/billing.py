"""
Тарифы и лимиты.

Free тариф создаётся автоматически при первой регистрации владельца —
делается в onboarding/owner_bot. Без подписки получаем дефолт «free, 2 машины».
"""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Subscription, Vehicle


@dataclass(frozen=True)
class Plan:
    code: str
    title: str
    vehicles_limit: int
    price_rub: int  # 0 = бесплатно


PLANS: dict[str, Plan] = {
    "free": Plan("free", "FREE", 2, 0),
    "base": Plan("base", "BASE", 5, 1490),
    "business": Plan("business", "BUSINESS", 15, 2990),
    "pro": Plan("pro", "PRO", 30, 4990),
}


async def get_or_create_subscription(
    session: AsyncSession, owner_id: int
) -> Subscription:
    sub = await session.execute(
        select(Subscription).where(Subscription.owner_id == owner_id)
    )
    existing = sub.scalar_one_or_none()
    if existing is not None:
        return existing
    fresh = Subscription(
        owner_id=owner_id, plan="free", vehicles_limit=PLANS["free"].vehicles_limit
    )
    session.add(fresh)
    await session.commit()
    return fresh


async def can_add_vehicle(session: AsyncSession, owner_id: int) -> tuple[bool, int, int]:
    """Возвращает (можно_добавить, текущее_количество, лимит)."""
    sub = await get_or_create_subscription(session, owner_id)
    count = (
        await session.execute(
            select(func.count(Vehicle.id)).where(
                Vehicle.owner_id == owner_id, Vehicle.is_active.is_(True)
            )
        )
    ).scalar_one() or 0
    return (count < sub.vehicles_limit, int(count), sub.vehicles_limit)


def format_tariffs() -> str:
    lines = ["💎 <b>Тарифы Автопарк TMS</b>\n"]
    for plan in PLANS.values():
        price = "<b>бесплатно</b>" if plan.price_rub == 0 else f"<b>{plan.price_rub} ₽/мес</b>"
        lines.append(f"<b>{plan.title}</b> — до {plan.vehicles_limit} машин · {price}")
    lines.append("")
    lines.append("Подключение через личный контакт — нажмите кнопку ниже.")
    return "\n".join(lines)
