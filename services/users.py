from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import Session
from models import User
from config import config


async def get_or_create_user(session: Session, telegram_id: int) -> User:
    """Fetch a user by Telegram ID, creating one if missing.

    Safe for concurrent calls (prevents UNIQUE constraint crash).
    """
    tg_id = int(telegram_id)

    # 1) Try fetch first
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user:
        return user

    admin_ids = set(getattr(config, "ADMIN_IDS", []) or [])
    is_admin = tg_id in admin_ids

    # 2) Try create
    user = User(
        telegram_id=tg_id,
        is_banned=False,
        access_granted=True,  # доступ по умолчанию; админ может отозвать в панели
        is_admin=is_admin,
    )
    session.add(user)

    try:
        await session.commit()
    except IntegrityError:
        # Another concurrent request created this user already
        await session.rollback()
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user2 = result.scalar_one_or_none()
        if user2:
            return user2
        raise

    await session.refresh(user)
    return user
