"""Личный ЧС продавцов: не валидировать повторно на другом объявлении; строгий матч GAG."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete, func, select as sa_select

from models import Offer, OfferEmail, SellerBlacklist
from services.offer_matching import canon_seller_email
from services.offer_storage import link_key, offer_fingerprint


async def is_seller_blacklisted(session, user_id: int, seller_email: str) -> bool:
    canon = canon_seller_email(seller_email)
    if not canon:
        return False
    row = (
        await session.execute(
            sa_select(SellerBlacklist.id)
            .where(SellerBlacklist.user_id == int(user_id))
            .where(func.lower(SellerBlacklist.seller_email) == canon)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def list_seller_blacklist(session, user_id: int, *, limit: int = 50) -> list[SellerBlacklist]:
    return list(
        (
            await session.execute(
                sa_select(SellerBlacklist)
                .where(SellerBlacklist.user_id == int(user_id))
                .order_by(SellerBlacklist.id.desc())
                .limit(int(limit))
            )
        ).scalars().all()
    )


async def add_seller_blacklist(
    session,
    user_id: int,
    seller_email: str,
    *,
    note: str | None = None,
) -> tuple[bool, str]:
    canon = canon_seller_email(seller_email)
    if not canon or "@" not in canon:
        return False, "Некорректный email"
    if await is_seller_blacklisted(session, user_id, canon):
        return False, "Уже в ЧС"
    session.add(
        SellerBlacklist(
            user_id=int(user_id),
            seller_email=canon,
            note=(note or "").strip() or None,
        )
    )
    await session.flush()
    return True, canon


async def remove_seller_blacklist(session, user_id: int, row_id: int) -> bool:
    res = await session.execute(
        sa_delete(SellerBlacklist)
        .where(SellerBlacklist.id == int(row_id))
        .where(SellerBlacklist.user_id == int(user_id))
    )
    return bool(res.rowcount)


async def load_seller_email_offer_map(session, user_id: int) -> dict[str, set[str]]:
    """Какие ссылки объявлений уже привязаны к email продавца в БД."""
    rows = (
        await session.execute(
            sa_select(OfferEmail.email, Offer.link)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == int(user_id))
        )
    ).all()
    out: dict[str, set[str]] = {}
    for em, link in rows:
        canon = canon_seller_email(str(em or ""))
        lk = link_key(str(link or ""))
        if not canon:
            continue
        out.setdefault(canon, set())
        if lk:
            out[canon].add(lk)
    return out


def item_link_key(item: dict) -> str:
    return link_key(
        str(item.get("item_link") or item.get("link") or item.get("url") or "")
    )


def emails_from_item_dict(item: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in ("validated_emails", "emails", "email", "seller_email", "from_email"):
        raw = item.get(key)
        if isinstance(raw, list):
            for e in raw:
                c = canon_seller_email(str(e or ""))
                if c and c not in seen:
                    seen.add(c)
                    out.append(c)
        elif isinstance(raw, str) and raw.strip():
            c = canon_seller_email(raw)
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def should_skip_validation_item_sync(
    item: dict,
    *,
    email_offer_map: dict[str, set[str]],
    blacklist_emails: set[str],
    batch_email_links: dict[str, str] | None = None,
) -> tuple[bool, str]:
    lk = item_link_key(item)
    for em in emails_from_item_dict(item):
        if em in blacklist_emails:
            return True, "в ЧС"
        prev = email_offer_map.get(em) or set()
        if prev and lk and lk not in prev:
            return True, "другой лот в БД"
        if batch_email_links and em in batch_email_links:
            if lk and batch_email_links[em] != lk:
                return True, "другой лот в файле"
    return False, ""


async def should_skip_validation_item(
    session,
    user_id: int,
    item: dict,
    *,
    email_offer_map: dict[str, set[str]] | None = None,
    batch_email_links: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """
    Пропустить валидацию, если продавец в ЧС или уже валидирован на другом объявлении.
  batch_email_links: email -> link_key в текущем файле (первый выигрывает).
    """
    if email_offer_map is None:
        email_offer_map = await load_seller_email_offer_map(session, user_id)

    lk = item_link_key(item)
    for em in emails_from_item_dict(item):
        if await is_seller_blacklisted(session, user_id, em):
            return True, "в ЧС"
        prev = email_offer_map.get(em) or set()
        if prev and lk and lk not in prev:
            return True, "другой лот в БД"
        if batch_email_links and em in batch_email_links:
            if lk and batch_email_links[em] != lk:
                return True, "другой лот в файле"

    return False, ""


async def register_validated_seller_email(
    session,
    user_id: int,
    seller_email: str,
    item: dict,
    *,
    auto_blacklist_on_conflict: bool = True,
) -> None:
    """После успешной валидации: если email уже на другом лоте — в ЧС."""
    canon = canon_seller_email(seller_email)
    lk = item_link_key(item)
    if not canon or not lk:
        return
    email_map = await load_seller_email_offer_map(session, user_id)
    prev = email_map.get(canon) or set()
    if prev and lk not in prev and auto_blacklist_on_conflict:
        await add_seller_blacklist(
            session,
            user_id,
            canon,
            note=f"авто: другой лот ({lk[:40]})",
        )
