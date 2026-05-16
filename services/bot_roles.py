"""Роли: config.ADMIN_IDS и is_admin в Postgres."""

from __future__ import annotations

from config import config
from database import Session
from services.users import get_or_create_user


def config_admin_ids() -> set[int]:
    return {int(x) for x in (getattr(config, "ADMIN_IDS", []) or [])}


async def user_is_admin(telegram_id: int) -> bool:
    tg_id = int(telegram_id)
    if tg_id in config_admin_ids():
        return True
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
    return bool(getattr(user, "is_admin", False))
