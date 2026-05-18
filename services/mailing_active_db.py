"""Флаг активной рассылки в Postgres — виден и bot.py, и imap_worker (разные процессы)."""
from __future__ import annotations

from sqlalchemy import select

from database import db_session
from models import User, UserSetting
from services.users import get_or_create_user
from services.user_settings import set_user_setting

MAILING_ACTIVE_KEY = "mailing_active"


async def set_mailing_active(telegram_id: int, *, active: bool) -> None:
    async with db_session() as session:
        user = await get_or_create_user(session, int(telegram_id))
        await set_user_setting(session, user, MAILING_ACTIVE_KEY, "1" if active else "0")


async def is_user_mailing_active(telegram_id: int) -> bool:
    """Активна ли рассылка /send у пользователя (память процесса + флаг в БД)."""
    from services.sending_state import get_sending_state

    tid = int(telegram_id)
    st = get_sending_state(tid)
    if st and bool(st.is_running) and not bool(st.is_stopping):
        return True
    return tid in await mailing_telegram_ids_from_db()


async def mailing_telegram_ids_from_db() -> frozenset[int]:
    async with db_session() as session:
        rows = (
            await session.execute(
                select(User.telegram_id)
                .join(UserSetting, UserSetting.user_id == User.id)
                .where(UserSetting.key == MAILING_ACTIVE_KEY, UserSetting.value == "1")
            )
        ).scalars().all()
    out: set[int] = set()
    for tid in rows:
        if tid is not None:
            out.add(int(tid))
    return frozenset(out)
