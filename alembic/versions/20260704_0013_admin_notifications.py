"""админы: флаг «получать уведомления бота»

Revision ID: 0013_admin_notifications
Revises: 0012_web_sessions
Create Date: 2026-07-04

У владельца несколько устройств: второй телефон — отдельный Telegram-аккаунт,
добавленный админом кабинета. Уведомления владельцу теперь дублируются всем
админам; этим флагом отдельного админа можно отключить от рассылки.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_admin_notifications"
down_revision: Union[str, None] = "0012_web_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "admins",
        sa.Column(
            "notifications_enabled", sa.Boolean(),
            nullable=False, server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("admins", "notifications_enabled")
