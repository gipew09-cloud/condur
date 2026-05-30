"""initial — bootstrap из текущих моделей

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-30

Стартовая идемпотентная миграция.

Почему так: на момент перехода на Alembic у нас уже есть рабочая БД на
Railway, заполненная через прежний create_db.py + раздачу ALTER'ов.
Если первая миграция будет «честно» делать op.create_table — упадёт
с «table already exists». Поэтому используем Base.metadata.create_all
с checkfirst=True — на существующей БД no-op, на пустой создаст всё.

Сразу после этого Alembic запишет ревизию 0001_initial в alembic_version
и дальше любые изменения моделей будут отдельными autogenerate-миграциями.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.models import Base  # импорт внутри, чтобы alembic не падал при сборе ревизий
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    from app.models import Base
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
