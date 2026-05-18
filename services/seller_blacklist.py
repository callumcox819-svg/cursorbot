"""Личный ЧС имён продавцов из JSON: повторно не валидировать у того же пользователя."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete, func, select as sa_select

from models import Offer, SellerBlacklist
from services.seller_name import normalize_seller_name, seller_name_from_item


def seller_name_key(raw: str) -> str:
    """Ключ для сравнения: Maria Johansen → maria johansen."""
    return normalize_seller_name(raw).strip().lower()


def seller_name_key_from_item(item: dict) -> str:
    return seller_name_key(seller_name_from_item(item))


async def load_seller_name_keys(session, user_id: int) -> set[str]:
    """Имена продавцов, которых уже не валидируем: ЧС в БД + person_name из офферов."""
    keys: set[str] = set()
    rows = (
        await session.execute(
            sa_select(SellerBlacklist.seller_name_key).where(SellerBlacklist.user_id == int(user_id))
        )
    ).all()
    for (k,) in rows:
        if k:
            keys.add(str(k).strip().lower())

    off_names = (
        await session.execute(
            sa_select(Offer.person_name).where(Offer.user_id == int(user_id)).where(Offer.person_name.is_not(None))
        )
    ).all()
    for (nm,) in off_names:
        key = seller_name_key(str(nm or ""))
        if key:
            keys.add(key)
    return keys


async def is_seller_name_blacklisted(session, user_id: int, seller_name: str) -> bool:
    key = seller_name_key(seller_name)
    if not key:
        return False
    row = (
        await session.execute(
            sa_select(SellerBlacklist.id)
            .where(SellerBlacklist.user_id == int(user_id))
            .where(func.lower(SellerBlacklist.seller_name_key) == key)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def add_seller_name_blacklist(
    session,
    user_id: int,
    seller_name: str,
) -> bool:
    key = seller_name_key(seller_name)
    if not key:
        return False
    if await is_seller_name_blacklisted(session, user_id, seller_name):
        return False
    display = normalize_seller_name(seller_name).strip() or key
    session.add(
        SellerBlacklist(
            user_id=int(user_id),
            seller_name_key=key,
            seller_name_display=display,
        )
    )
    await session.flush()
    return True
