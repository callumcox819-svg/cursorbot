"""Сохранение объявлений из JSON парсера в БД (все поля + email после валидации)."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import func, or_ as sa_or, select as sa_select

from models import Offer, OfferEmail

_LINK_QS_RE = re.compile(r"\?.*$")


def link_key(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    if not u:
        return ""
    u = _LINK_QS_RE.sub("", u)
    return u


def offer_fingerprint(item: dict[str, Any]) -> str:
    lk = link_key(str(item.get("item_link") or item.get("link") or ""))
    if lk:
        return f"link:{lk}"
    title = str(item.get("item_title") or item.get("title") or "").strip().lower()[:120]
    name = str(item.get("item_person_name") or item.get("person_name") or item.get("name") or "").strip().lower()[:80]
    return f"t:{title}|n:{name}"


def _title_from_item_dict(item: dict[str, Any]) -> str:
    """Название товара из VOID/парсера — разные ключи и вложенный void."""
    if not isinstance(item, dict):
        return ""
    for key in (
        "item_title",
        "title",
        "product_title",
        "ad_title",
        "offer_title",
        "name_title",
    ):
        v = item.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    void = item.get("void")
    if isinstance(void, dict):
        t = _title_from_item_dict(void)
        if t:
            return t
    return ""


def fields_from_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "person_name": str(
            item.get("item_person_name")
            or item.get("person_name")
            or item.get("seller_name")
            or item.get("name")
            or ""
        ).strip(),
        "title": _title_from_item_dict(item),
        "price": str(item.get("item_price") or item.get("price") or "").strip(),
        "link": str(item.get("item_link") or item.get("link") or item.get("url") or "").strip(),
        "photo": str(
            item.get("item_photo") or item.get("photo") or item.get("image") or item.get("img") or ""
        ).strip(),
    }


def parse_offer_raw(raw_json: str | None) -> dict[str, Any]:
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first_raw_str(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        v = str(raw.get(key) or "").strip()
        if v:
            return v
    return ""


def offer_effective_price(offer: Offer | None, *, default: str = "0") -> str:
    """Цена для GAG/карточки: колонка Offer.price, иначе item_price/price из raw_json, иначе default."""
    if not offer:
        return default
    p = str(getattr(offer, "price", None) or "").strip()
    if p:
        return p
    raw = parse_offer_raw(getattr(offer, "raw_json", None))
    v = _first_raw_str(raw, ("item_price", "price"))
    return v or default


def offer_effective_title(offer: Offer | None) -> str:
    """Название: Offer.title, иначе item_title/title из raw_json."""
    if not offer:
        return ""
    t = str(getattr(offer, "title", None) or "").strip()
    if t:
        return t
    raw = parse_offer_raw(getattr(offer, "raw_json", None))
    return _first_raw_str(
        raw,
        ("item_title", "title", "product_title", "ad_title", "offer_title"),
    )


def _title_compact(s: str) -> str:
    """Сравнение темы Re: и названия лота (umlauts, пробелы, /)."""
    from services.offer_matching import _fold_de, _norm_subject

    s = _fold_de(_norm_subject(s))
    return re.sub(r"[\s\-–—/\\.,:;!?+|]+", "", s)


async def find_offer_by_incoming_subject(
    session,
    *,
    user_id: int,
    subject: str,
    from_name: str = "",
) -> Offer | None:
    """
    Прямой матч темы ответа к Offer.title / raw_json (Re: «Gabel-Schlüssel 32 / 36»).
    Нужен, когда продавец отвечает с реального Gmail, а в OfferEmail — валидированный адрес.
    """
    from difflib import SequenceMatcher

    from services.offer_matching import (
        _subject_title_conflicts,
        _subject_tokens,
        list_offers_for_seller_name,
        normalized_reply_subject,
        subject_is_informative,
    )

    if not subject_is_informative(subject):
        return None

    subj_norm = normalized_reply_subject(subject)
    subj_compact = _title_compact(subj_norm)
    if len(subj_compact) < 8:
        return None

    name_offs: list[Offer] = []
    if (from_name or "").strip():
        name_offs = await list_offers_for_seller_name(
            session, user_id=int(user_id), from_name=from_name
        )
    pool = name_offs if name_offs else (
        await session.execute(
            sa_select(Offer)
            .where(Offer.user_id == int(user_id))
            .order_by(Offer.id.desc())
            .limit(1200)
        )
    ).scalars().all()

    best: Offer | None = None
    best_key = 0.0

    for off in pool:
        title = offer_effective_title(off)
        if not title or _subject_title_conflicts(subj_norm, title):
            continue
        title_compact = _title_compact(title)
        if not title_compact:
            continue

        score = 0.0
        if subj_compact == title_compact:
            score = 200.0
        elif title_compact in subj_compact or subj_compact in title_compact:
            score = 150.0 + min(len(title_compact), len(subj_compact)) * 0.1
        else:
            ratio = SequenceMatcher(None, subj_compact, title_compact).ratio()
            if ratio < 0.72:
                continue
            score = 80.0 * ratio

        subj_l = subj_norm.lower()
        title_l = title.lower()
        if subj_l in title_l or title_l in subj_l:
            score += 40.0

        if score > best_key:
            best_key = score
            best = off

    if best and best_key >= 50.0:
        return best

    # SQL-префильтр по первому слову темы (gabel, couch, …)
    word_toks = [t for t in _subject_tokens(subj_norm) if len(t) >= 4 and not t.isdigit()]
    if word_toks:
        pat = f"%{word_toks[0]}%"
        sql_rows = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.user_id == int(user_id))
                .where(
                    sa_or(
                        func.lower(Offer.title).like(pat),
                        func.lower(Offer.raw_json).like(pat),
                    )
                )
                .order_by(Offer.id.desc())
                .limit(40)
            )
        ).scalars().all()
        for off in sql_rows:
            title = offer_effective_title(off)
            if not title or _subject_title_conflicts(subj_norm, title):
                continue
            tc = _title_compact(title)
            if not tc:
                continue
            if tc == subj_compact or tc in subj_compact or subj_compact in tc:
                return off

    return None


async def find_offer_by_inbox_pinned_subject(
    session,
    *,
    user_id: int,
    inbox_email: str,
    subject: str,
) -> Offer | None:
    """
    Лот, на который уже писали с этого ящика (pinned в другом диалоге),
    если продавец ответил с другого email (Gmail vs валидированный).
    """
    from models import ConversationLink
    from services.offer_matching import (
        _subject_title_conflicts,
        normalized_reply_subject,
        subject_is_informative,
    )

    if not subject_is_informative(subject):
        return None
    inbox = (inbox_email or "").strip().lower()
    if not inbox:
        return None
    subj_norm = normalized_reply_subject(subject)

    rows = (
        await session.execute(
            sa_select(Offer)
            .join(ConversationLink, ConversationLink.pinned_offer_id == Offer.id)
            .where(ConversationLink.user_id == int(user_id))
            .where(func.lower(ConversationLink.account_email) == inbox)
            .where(ConversationLink.pinned_offer_id.is_not(None))
            .order_by(ConversationLink.id.desc())
            .limit(50)
        )
    ).scalars().all()

    subj_c = _title_compact(subj_norm)
    for off in rows:
        title = offer_effective_title(off)
        if not title or _subject_title_conflicts(subj_norm, title):
            continue
        tc = _title_compact(title)
        if tc and (tc == subj_c or tc in subj_c or subj_c in tc):
            return off
    return None


async def find_offer_by_link(session, *, user_id: int, ad_url: str) -> Offer | None:
    """Offer по ссылке объявления (нормализованный link_key)."""
    lk = link_key(ad_url)
    if not lk:
        return None
    rows = (
        await session.execute(
            sa_select(Offer)
            .where(Offer.user_id == int(user_id))
            .where(Offer.link.is_not(None))
            .order_by(Offer.id.desc())
            .limit(800)
        )
    ).scalars().all()
    for off in rows:
        if link_key(str(off.link or "")) == lk:
            return off
    return None


def offer_effective_photo(offer: Offer | None) -> str:
    """Фото: Offer.photo, иначе item_photo/photo/image/img из raw_json."""
    if not offer:
        return ""
    p = str(getattr(offer, "photo", None) or "").strip()
    if p:
        return p
    raw = parse_offer_raw(getattr(offer, "raw_json", None))
    return _first_raw_str(raw, ("item_photo", "photo", "image", "img"))


def index_validated_rows(validated: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Индекс результатов валидации email по ссылке объявления."""
    out: dict[str, dict[str, Any]] = {}
    for row in validated or []:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else row
        if not isinstance(raw, dict):
            continue
        key = offer_fingerprint(raw)
        if key:
            out[key] = row
        lk = link_key(str(raw.get("item_link") or raw.get("link") or ""))
        if lk:
            out[f"link:{lk}"] = row
    return out


def emails_from_validated_row(row: dict[str, Any] | None, norm_email) -> list[str]:
    if not row:
        return []
    picked: list[str] = []
    seen: set[str] = set()
    for e in row.get("emails") or []:
        e2 = norm_email(str(e or ""))
        if not e2 or e2 in seen:
            continue
        seen.add(e2)
        picked.append(e2)
    return picked


async def save_all_offers_from_import(
    session,
    *,
    user_id: int,
    items: list[dict[str, Any]],
    validated_rows: list[dict[str, Any]],
    norm_email,
    max_emails_per_offer: int = 2,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """
    Сохранить ВСЕ объявления из файла.
    Returns: (offers_saved, offers_with_email, email_rows_saved, output_json_rows)
    """
    vindex = index_validated_rows(validated_rows)
    offers_saved = 0
    offers_with_email = 0
    email_rows_saved = 0
    output_rows: list[dict[str, Any]] = []

    for it in items:
        if not isinstance(it, dict):
            continue
        fp = offer_fingerprint(it)
        vrow = vindex.get(fp)
        if not vrow:
            lk = link_key(str(it.get("item_link") or it.get("link") or ""))
            if lk:
                vrow = vindex.get(f"link:{lk}")

        fields = fields_from_item(it)
        picked = emails_from_validated_row(vrow, norm_email)

        # 100% полей из парсера — для генерации ссылок и матча по всем данным.
        payload = json.loads(json.dumps(it, ensure_ascii=False, default=str))
        if isinstance(payload, dict):
            payload.setdefault(
                "item_person_name",
                str(
                    it.get("item_person_name")
                    or it.get("person_name")
                    or it.get("name")
                    or ""
                ).strip(),
            )
        else:
            payload = dict(it)
        if picked:
            payload["validated_emails"] = list(picked)

        offer = Offer(
            user_id=int(user_id),
            person_name=fields["person_name"] or None,
            title=fields["title"] or None,
            price=fields["price"] or None,
            link=fields["link"] or None,
            photo=fields["photo"] or None,
            raw_json=json.dumps(payload, ensure_ascii=False),
        )
        session.add(offer)
        await session.flush()
        offers_saved += 1

        if picked:
            offers_with_email += 1
            for em in picked[:max_emails_per_offer]:
                session.add(OfferEmail(offer_id=offer.id, email=em))
                email_rows_saved += 1

        payload["offer_id"] = int(offer.id)
        output_rows.append(payload)

    return offers_saved, offers_with_email, email_rows_saved, output_rows
