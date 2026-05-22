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
        "link": str(
            item.get("item_link")
            or item.get("link")
            or item.get("url")
            or item.get("ad_url")
            or item.get("marketplace_link")
            or ""
        ).strip(),
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


def offer_effective_link(offer: Offer | None) -> str:
    """Ссылка объявления: Offer.link, иначе item_link/link из raw_json."""
    if not offer:
        return ""
    lk = str(getattr(offer, "link", None) or "").strip()
    if lk:
        return lk
    raw = parse_offer_raw(getattr(offer, "raw_json", None))
    return _first_raw_str(
        raw,
        ("item_link", "link", "url", "ad_url", "marketplace_link"),
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
    from_email: str = "",
) -> Offer | None:
    """
    Прямой матч темы ответа к Offer.title / raw_json (Re: «Gabel-Schlüssel 32 / 36»).
    Нужен, когда продавец отвечает с реального Gmail, а в OfferEmail — валидированный адрес.
    """
    from difflib import SequenceMatcher

    from services.offer_matching import (
        _canon_email,
        _fold_match_text,
        _subject_title_conflicts,
        _subject_tokens,
        list_offers_for_incoming_contact,
        list_offers_for_seller_name,
        normalized_reply_subject,
        subject_is_informative,
    )

    if not subject_is_informative(subject):
        return None

    subj_norm = normalized_reply_subject(subject)
    subj_compact = _title_compact(subj_norm)
    if len(subj_compact) < 4:
        return None

    fe_can = _canon_email(from_email)
    pool: list[Offer] = []
    if fe_can and "@" in fe_can:
        pool = await list_offers_for_incoming_contact(
            session,
            user_id=int(user_id),
            from_email=from_email,
            from_name="",
        )
    if not pool and (from_name or "").strip() and not fe_can:
        pool = await list_offers_for_seller_name(
            session, user_id=int(user_id), from_name=from_name
        )
    if not pool and fe_can:
        return None
    if not pool:
        pool = (
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

    word_toks = [t for t in _subject_tokens(subj_norm) if len(t) >= 4]
    all_rows = (
        await session.execute(
            sa_select(Offer)
            .where(Offer.user_id == int(user_id))
            .order_by(Offer.id.desc())
            .limit(500)
        )
    ).scalars().all()
    for off in all_rows:
        title = offer_effective_title(off)
        if not title or _subject_title_conflicts(subj_norm, title):
            continue
        tf = _fold_match_text(title)
        if word_toks and not all(w in tf for w in word_toks):
            continue
        tc = _title_compact(title)
        if tc and (tc == subj_compact or tc in subj_compact or subj_compact in tc):
            return off
        if word_toks and len(word_toks) >= 2:
            return off

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


async def find_offer_by_subject_aggressive(
    session,
    *,
    user_id: int,
    subject: str,
    from_name: str = "",
) -> Offer | None:
    """Жёсткий поиск: все значимые слова темы должны быть в title или raw_json."""
    from services.offer_matching import (
        _fold_match_text,
        _subject_title_conflicts,
        _subject_tokens,
        list_offers_for_seller_name,
        normalized_reply_subject,
        subject_is_informative,
    )

    if not subject_is_informative(subject):
        return None
    subj_norm = normalized_reply_subject(subject)
    toks = [t for t in _subject_tokens(subj_norm) if len(t) >= 3]
    word_toks = [t for t in toks if len(t) >= 4]
    if not word_toks:
        return None

    stmt = sa_select(Offer).where(Offer.user_id == int(user_id))
    for t in word_toks[:4]:
        pat = f"%{t}%"
        stmt = stmt.where(
            sa_or(
                func.lower(Offer.title).like(pat),
                func.lower(Offer.raw_json).like(pat),
            )
        )
    rows = (await session.execute(stmt.order_by(Offer.id.desc()).limit(15))).scalars().all()
    if not rows and (from_name or "").strip():
        pool = await list_offers_for_seller_name(
            session, user_id=int(user_id), from_name=from_name
        )
        rows = [
            o
            for o in pool
            if all(w in _fold_match_text(offer_effective_title(o)) for w in word_toks)
        ]

    subj_c = _title_compact(subj_norm)
    best: Offer | None = None
    best_sc = 0.0
    for off in rows:
        title = offer_effective_title(off)
        if not title or _subject_title_conflicts(subj_norm, title):
            continue
        tf = _fold_match_text(title)
        if not all(w in tf for w in word_toks):
            continue
        tc = _title_compact(title)
        sc = float(hits) * 20.0
        if tc and (tc == subj_c or tc in subj_c or subj_c in tc):
            sc += 100.0
        if sc > best_sc:
            best_sc = sc
            best = off
    return best


async def diagnose_subject_match(
    session,
    *,
    user_id: int,
    subject: str,
) -> dict[str, Any]:
    """Подсказка в ошибке: есть ли похожие лоты в БД."""
    from services.offer_matching import _fold_match_text, normalized_reply_subject, _subject_tokens

    total = (
        await session.execute(
            sa_select(func.count(Offer.id)).where(Offer.user_id == int(user_id))
        )
    ).scalar() or 0
    subj_norm = normalized_reply_subject(subject)
    word_toks = [t for t in _subject_tokens(subj_norm) if len(t) >= 4]
    near = 0
    samples: list[str] = []
    rows = (
        await session.execute(
            sa_select(Offer)
            .where(Offer.user_id == int(user_id))
            .order_by(Offer.id.desc())
            .limit(500)
        )
    ).scalars().all()
    for off in rows:
        title = offer_effective_title(off)
        raw = parse_offer_raw(getattr(off, "raw_json", None))
        hay = _fold_match_text(title)
        if raw:
            hay += " " + _fold_match_text(
                " ".join(
                    str(raw.get(k) or "")
                    for k in (
                        "item_title",
                        "title",
                        "item_desc",
                        "item_person_name",
                        "person_name",
                    )
                )
            )
        if not hay.strip():
            continue
        if word_toks and all(w in hay for w in word_toks):
            near += 1
            if len(samples) < 3:
                samples.append((title or str(raw.get("item_title") or ""))[:50])
    return {
        "total": int(total),
        "near": int(near),
        "samples": samples,
        "words": word_toks[:4],
    }


async def offer_bound_to_validated_email(
    session,
    *,
    user_id: int,
    offer: Offer | None,
    contact_email: str,
) -> bool:
    """Лот привязан к from_email: OfferEmail и/или validated_emails в raw_json (после валидации)."""
    if not offer:
        return False
    from services.offer_matching import _canon_email, offer_has_validated_email_in_raw

    fe = _canon_email(contact_email)
    if not fe or "@" not in fe:
        return True
    ids = await _offer_ids_with_email(session, int(user_id), fe)
    if int(offer.id) in ids:
        return True
    return offer_has_validated_email_in_raw(offer, contact_email)


async def _offer_ids_with_email(session, user_id: int, fe_can: str) -> set[int]:
    """Offer.id, у которых есть строка OfferEmail с этим адресом."""
    from services.offer_matching import _canon_email

    if not fe_can:
        return set()
    rows = (
        await session.execute(
            sa_select(OfferEmail.offer_id, OfferEmail.email)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == int(user_id))
            .order_by(OfferEmail.id.desc())
            .limit(3000)
        )
    ).all()
    out: set[int] = set()
    for oid, em in rows:
        if _canon_email(str(em or "")) == fe_can:
            out.add(int(oid))
    return out


async def find_offer_by_incoming_signals(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str = "",
    from_name: str = "",
    body_text: str = "",
    ad_url: str | None = None,
    product_title: str | None = None,
) -> Offer | None:
    """
    Точный лот для входящего письма / «Создать ссылку»:
    validated email + имя продавца + тема + ссылка + поля raw_json.
    """
    from services.offer_matching import (
        _canon_email,
        _fold_match_text,
        _ratio,
        _subject_distinct_tokens,
        _subject_title_conflicts,
        _subject_tokens,
        list_offers_for_incoming_contact,
        normalized_reply_subject,
        offer_has_contact_email,
        offer_matches_incoming_subject,
        score_offer,
        subject_is_informative,
        subject_match_score,
    )

    from services.offer_matching import offer_acceptable_for_subject

    fe_can = _canon_email(from_email)
    subj_norm = normalized_reply_subject(subject)
    title_hint = (product_title or subj_norm or "").strip()
    email_offer_ids = await _offer_ids_with_email(session, int(user_id), fe_can) if fe_can else set()

    if subj_norm and subject_is_informative(subject):
        off_subj = await find_offer_by_incoming_subject(
            session,
            user_id=int(user_id),
            subject=subject,
            from_name=from_name,
            from_email=from_email,
        )
        if off_subj and offer_acceptable_for_subject(off_subj, subject):
            if not fe_can or int(off_subj.id) in email_offer_ids or offer_has_contact_email(
                off_subj, from_email
            ):
                return off_subj

    candidates: list[Offer] = []
    seen: set[int] = set()

    def _add(off: Offer | None) -> None:
        if not off:
            return
        oid = int(off.id)
        if oid in seen:
            return
        seen.add(oid)
        candidates.append(off)

    if (ad_url or "").strip():
        by_url = await find_offer_by_link(session, user_id=int(user_id), ad_url=ad_url or "")
        _add(by_url)

    for off in await list_offers_for_incoming_contact(
        session,
        user_id=int(user_id),
        from_email=from_email,
        from_name=from_name,
    ):
        _add(off)

    if not candidates:
        return None

    if len(candidates) == 1:
        only = candidates[0]
        if fe_can and int(only.id) not in email_offer_ids and not offer_has_contact_email(
            only, from_email
        ):
            return None
        if not offer_acceptable_for_subject(only, subject):
            return None
        return only

    best: Offer | None = None
    best_sc = -1.0
    url_lk = link_key(ad_url or "")

    distinct = _subject_distinct_tokens(subj_norm) if subj_norm else []

    for off in candidates:
        if fe_can and int(off.id) not in email_offer_ids and not offer_has_contact_email(
            off, from_email
        ):
            continue
        title = offer_effective_title(off)
        if not offer_acceptable_for_subject(off, subject):
            continue
        if distinct:
            tf = _fold_match_text(title)
            if not all(t in tf for t in distinct):
                continue

        email_hit = bool(
            fe_can
            and (int(off.id) in email_offer_ids or offer_has_contact_email(off, from_email))
        )
        sc = score_offer(
            off,
            from_email=from_email,
            subject=subject,
            from_name=from_name,
            body_text=body_text,
            email_hit=email_hit,
        )
        if subj_norm:
            sc += subject_match_score(subject, off) * 0.65
        if title_hint and title:
            sc += 55.0 * _ratio(title_hint, title)
            th = title_hint.lower()
            tl = title.lower()
            if th in tl or tl in th:
                sc += 40.0
        off_lk = link_key(offer_effective_link(off))
        if url_lk and off_lk and url_lk == off_lk:
            sc += 280.0

        if sc > best_sc:
            best_sc = sc
            best = off

    if not best and (from_name or "").strip() and subj_norm and not fe_can:
        name_offs = await list_offers_for_incoming_contact(
            session,
            user_id=int(user_id),
            from_email=from_email,
            from_name=from_name,
        )
        subj_c = _title_compact(subj_norm)
        for off in name_offs:
            title = offer_effective_title(off)
            if not title:
                continue
            tc = _title_compact(title)
            if subj_c and tc and (tc == subj_c or tc in subj_c or subj_c in tc):
                return off
            tf = _fold_match_text(title)
            if all(w in tf for w in _subject_tokens(subj_norm) if len(w) >= 4):
                return off

    if not best:
        return None

    min_sc = 90.0 if fe_can else 125.0
    if best_sc < min_sc:
        return None

    email_hit_best = bool(
        fe_can
        and (int(best.id) in email_offer_ids or offer_has_contact_email(best, fe_can))
    )
    if subject_is_informative(subject) and subj_norm:
        if not email_hit_best and not offer_matches_incoming_subject(best, subject):
            fn = (from_name or "").strip()
            pn = (best.person_name or "").strip()
            if not (fn and pn and _ratio(fn, pn) >= 0.75):
                return None
    return best


async def resolve_offer_from_saved_context(
    session,
    *,
    user_id: int,
    inbox_email: str,
    contact_email: str,
    subject: str = "",
    from_name: str = "",
    resolved_offer_id: int | None = None,
    ad_url: str | None = None,
) -> tuple[Offer | None, str]:
    """
    Уже сохранённый контекст: /send, pinned диалог, ad_url письма — без угадывания по 32 лотам.
    """
    from models import ConversationLink
    from services.offer_matching import (
        _canon_email,
        _ratio,
        _subject_title_conflicts,
        normalized_reply_subject,
        offer_acceptable_for_subject,
    )

    def _pair_saved(off: Offer | None, url: str, *, strict_subject: bool = True) -> tuple[Offer | None, str]:
        """Pin / журнал /send — доверяем сохранённому offer_id."""
        if not off:
            return None, ""
        u = (url or offer_effective_link(off) or "").strip()
        if not u:
            return None, ""
        if strict_subject:
            title = offer_effective_title(off)
            if subject and title and _subject_title_conflicts(subject, title):
                return None, ""
        return off, u

    from services.mailing_send_log import resolve_mailing_reply_context

    mctx = await resolve_mailing_reply_context(
        session,
        user_id=int(user_id),
        inbox_email=inbox_email,
        subject=subject,
        from_email=contact_email,
        from_name=from_name,
    )
    if mctx:
        url = (mctx.ad_url or offer_effective_link(mctx.offer) or "").strip()
        if url:
            return mctx.offer, url

    off_sig = await find_offer_by_incoming_signals(
        session,
        user_id=int(user_id),
        from_email=contact_email,
        subject=subject,
        from_name=from_name,
        product_title=normalized_reply_subject(subject) or None,
    )
    if off_sig:
        got = _pair_saved(off_sig, offer_effective_link(off_sig) or "")
        if got[0]:
            return got

    if resolved_offer_id:
        off = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.id == int(resolved_offer_id))
                .where(Offer.user_id == int(user_id))
                .limit(1)
            )
        ).scalars().first()
        got = _pair_saved(off, (ad_url or "").strip(), strict_subject=False)
        if got[0]:
            return got

    mail_url = (ad_url or "").strip()
    if mail_url:
        by_url = await find_offer_by_link(session, user_id=int(user_id), ad_url=mail_url)
        got = _pair_saved(by_url, mail_url)
        if got[0]:
            return got

    inbox = _canon_email(inbox_email)
    contact = _canon_email(contact_email)
    if inbox and contact:
        conv = (
            await session.execute(
                sa_select(ConversationLink)
                .where(ConversationLink.user_id == int(user_id))
                .where(func.lower(ConversationLink.account_email) == inbox)
                .where(func.lower(ConversationLink.from_email) == contact)
                .limit(1)
            )
        ).scalars().first()
        if conv:
            if (conv.ad_url or "").strip():
                cu = (conv.ad_url or "").strip()
                by_url = await find_offer_by_link(session, user_id=int(user_id), ad_url=cu)
                if by_url and await offer_bound_to_validated_email(
                    session,
                    user_id=int(user_id),
                    offer=by_url,
                    contact_email=contact_email,
                ):
                    got = _pair_saved(by_url, cu)
                    if got[0]:
                        return got
            if getattr(conv, "pinned_offer_id", None):
                off = (
                    await session.execute(
                        sa_select(Offer)
                        .where(Offer.id == int(conv.pinned_offer_id))
                        .where(Offer.user_id == int(user_id))
                        .limit(1)
                    )
                ).scalars().first()
                if off and offer_acceptable_for_subject(off, subject):
                    got = _pair_saved(off, (conv.ad_url or "").strip())
                    if got[0]:
                        return got

        fn = (from_name or "").strip().lower()
        if fn:
            conv_rows = (
                await session.execute(
                    sa_select(ConversationLink)
                    .where(ConversationLink.user_id == int(user_id))
                    .where(func.lower(ConversationLink.account_email) == inbox)
                    .where(ConversationLink.pinned_offer_id.is_not(None))
                    .order_by(ConversationLink.id.desc())
                    .limit(20)
                )
            ).scalars().all()
            for conv in conv_rows:
                off = (
                    await session.execute(
                        sa_select(Offer)
                        .where(Offer.id == int(conv.pinned_offer_id))
                        .where(Offer.user_id == int(user_id))
                        .limit(1)
                    )
                ).scalars().first()
                if not off:
                    continue
                pn = (off.person_name or "").strip().lower()
                if pn and (_ratio(fn, pn) >= 0.72 or fn in pn or pn in fn):
                    got = _pair_saved(off, (conv.ad_url or "").strip())
                    if got[0]:
                        return got

    return None, ""


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
        break
    return picked


def _scrub_raw_to_single_email(payload: dict[str, Any], keep: str, norm_email) -> None:
    """В raw_json остаётся одна валидная почта — без второго gmail/icloud из парсера."""
    kn = norm_email(str(keep or ""))
    if not kn or not isinstance(payload, dict):
        return
    payload["validated_emails"] = [kn]
    payload["validated_email"] = kn
    for key in (
        "email",
        "seller_email",
        "contact_email",
        "from_email",
        "owner_email",
        "account_email",
    ):
        v = payload.get(key)
        if isinstance(v, str) and v.strip() and norm_email(v) != kn:
            del payload[key]
    for key in ("emails", "seller_emails"):
        arr = payload.get(key)
        if isinstance(arr, list):
            payload[key] = [x for x in arr if norm_email(str(x or "")) == kn]


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
    seen_fp: set[str] = set()

    for it in items:
        if not isinstance(it, dict):
            continue
        fp = offer_fingerprint(it)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
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
            _scrub_raw_to_single_email(payload, picked[0], norm_email)

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
