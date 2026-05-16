from __future__ import annotations

from datetime import datetime
from sqlalchemy import select
from database import Session
from models import AppSetting


VALIDEMAIL_KEY_NAME = "validemail_api_key"


async def get_setting(session: Session, key: str) -> str | None:
    res = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = res.scalar_one_or_none()
    if row is None:
        return None
    val = (row.value or "").strip()
    return val or None


async def set_setting(session: Session, key: str, value: str | None) -> None:
    k = (key or "").strip()
    if not k:
        return
    res = await session.execute(select(AppSetting).where(AppSetting.key == k))
    row = res.scalar_one_or_none()
    if row is None:
        row = AppSetting(key=k, value=value, updated_at=datetime.utcnow())
        session.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
    await session.commit()


async def get_validemail_key(session: Session) -> str | None:
    return await get_setting(session, VALIDEMAIL_KEY_NAME)


async def set_validemail_key(session: Session, value: str | None) -> None:
    await set_setting(session, VALIDEMAIL_KEY_NAME, value)
