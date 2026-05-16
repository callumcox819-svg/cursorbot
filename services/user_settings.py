from __future__ import annotations

from typing import Any, Optional, Union, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import Session
from models import User, UserSetting


def _resolve_user_id(user_or_id: Union[int, str, User, Any]) -> int:
    """
    Принимает:
    - telegram user id (int/str),
    - ORM объект User,
    - или любой объект с `.id`.

    Возвращает int user_id для запросов.
    """
    if isinstance(user_or_id, User):
        return int(cast(int, user_or_id.id))

    if hasattr(user_or_id, "id") and getattr(user_or_id, "id") is not None:
        return int(getattr(user_or_id, "id"))

    return int(user_or_id)


async def get_user_setting(session: Session, user: Union[int, User], key: str) -> Optional[str]:
    user_id = _resolve_user_id(user)
    result = await session.execute(
        select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
    )
    row = result.scalar_one_or_none()
    return row.value if row else None


async def set_user_setting(session: Session, user: Union[int, User], key: str, value: str) -> None:
    user_id = _resolve_user_id(user)

    result = await session.execute(
        select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
    )
    row = result.scalar_one_or_none()

    if row:
        row.value = value
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise
        return

    # ВАЖНО: если в БД NOT NULL на html_nick/html_signature/sender_name — ставим безопасные дефолты явно.
    row = UserSetting(
        user_id=user_id,
        key=key,
        value=value,
        html_nick="",
        html_signature="",
        sender_name="",
    )
    session.add(row)

    try:
        await session.commit()
    except IntegrityError:
        # На случай гонки: кто-то вставил (user_id, key) одновременно
        await session.rollback()
        result = await session.execute(
            select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
        )
        row2 = result.scalar_one_or_none()
        if row2:
            row2.value = value
            await session.commit()
            return
        raise


async def delete_user_setting(session: Session, user: Union[int, User], key: str) -> None:
    user_id = _resolve_user_id(user)

    result = await session.execute(
        select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
    )
    row = result.scalar_one_or_none()
    if not row:
        return
    await session.delete(row)
    await session.commit()
