"""Журнал рассылки: offer_id + тема + ящик → точный лот при ответе продавца."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select as sa_select

from models import MailingSend, Offer, OfferEmail
from services.offer_matching import (
    _canon_email,
    _ratio,
    _subject_tokens,
    normalized_reply_subject,
    offer_matches_incoming_subject,
    subject_is_informative,
)
from services.offer_matching import _subject_title_conflicts
from services.offer_storage import (
    _title_compact,
    _offer_ids_with_email,
    offer_effective_link,
    offer_effective_photo,
    offer_effective_price,
    offer_effective_title,
)


def _service_from_link(link: str) -> str:
    l = (link or "").lower()
    if "ricardo.ch" in l:
        return "ricardo.ch"
    if "tutti.ch" in l:
        return "tutti.ch"
    if "post.ch" in l or "posta.ch" in l:
        return "post.ch"
    if "facebook.com" in l or "fb.com" in l:
        return "facebook.com"
    if "anibis.ch" in l:
        return "anibis.ch"
    return ""


def _snapshot_matches_reply(
    row: MailingSend,
    subject: str,
    *,
    offer: Offer | None = None,
) -> bool:
    """Тема ответа относится к лоту из этой рассылки (не путать с другим товаром)."""
    snap = (row.title_snapshot or "").strip()
    if not snap and offer:
        snap = (offer_effective_title(offer) or "").strip()
    if not snap:
        return False

    subj_norm = normalized_reply_subject(subject)
    subj_c = _title_compact(subj_norm) if subj_norm else ""
    sc = _title_compact(snap)
    if sc and subj_c and (sc == subj_c or sc in subj_c or subj_c in sc):
        return True

    sent_subj = normalized_reply_subject(row.subject or "")
    if sent_subj and subj_norm:
        sent_c = _title_compact(sent_subj)
        if sent_c and subj_c and (sent_c in subj_c or subj_c in sent_c or sent_c == subj_c):
            return True

    if snap and subj_norm and not _subject_title_conflicts(subject, snap):
        from services.offer_matching import _SUBJECT_WEAK, _fold_match_text

        folded_subj = _fold_match_text(subj_norm)
        strong = [
            t
            for t in _subject_tokens(folded_subj)
            if len(t) >= 4 and t not in _SUBJECT_WEAK
        ]
        if len(strong) >= 2 and sum(1 for t in strong if t in _fold_match_text(snap)) >= 2:
            return True
    return False


@dataclass
class MailingReplyContext:
    """Точный лот + снимок с момента /send (сервис, фото, цена — без угадываний)."""

    offer: Offer
    send_row: MailingSend | None
    offer_id: int
    product_title: str | None
    service_label: str | None
    photo_url: str | None
    offer_price: str | None
    ad_url: str | None


def _ctx_from_row(row: MailingSend, off: Offer) -> MailingReplyContext:
    from services.subject_offer import extract_core_offer_title_from_subject

    title = (row.title_snapshot or offer_effective_title(off) or "").strip()
    if not title:
        title = extract_core_offer_title_from_subject(row.subject or "")
    core = extract_core_offer_title_from_subject(row.subject or "")
    if core and title and title.lower().startswith(("kurze frage", "anfrage zu")):
        title = core
    title = title or None
    link = (row.ad_url_snapshot or offer_effective_link(off) or "").strip()
    svc = (row.service_label or "").strip() or _service_from_link(link) or None
    photo = (row.photo_url or offer_effective_photo(off) or "").strip() or None
    price = (row.offer_price or offer_effective_price(off, default="") or "").strip() or None
    return MailingReplyContext(
        offer=off,
        send_row=row,
        offer_id=int(off.id),
        product_title=title,
        service_label=svc,
        photo_url=photo,
        offer_price=price,
        ad_url=link or None,
    )


async def _load_offer(session, user_id: int, offer_id: int) -> Offer | None:
    return (
        await session.execute(
            sa_select(Offer)
            .where(Offer.id == int(offer_id))
            .where(Offer.user_id == int(user_id))
            .limit(1)
        )
    ).scalars().first()


async def record_mailing_send(
    session,
    *,
    user_id: int,
    offer_id: int,
    offer_email_id: int | None,
    inbox_email: str,
    to_email: str,
    subject: str,
    title_snapshot: str = "",
    offer: Offer | None = None,
) -> None:
    """Каждое успешное письмо /send — полный снимок лота для входящих ответов."""
    off = offer
    if not off:
        off = await _load_offer(session, int(user_id), int(offer_id))
    title = (title_snapshot or (offer_effective_title(off) if off else "") or "").strip()
    link = (offer_effective_link(off) if off else "") or ""
    row = MailingSend(
        user_id=int(user_id),
        offer_id=int(offer_id),
        offer_email_id=int(offer_email_id) if offer_email_id else None,
        inbox_email=_canon_email(inbox_email),
        to_email=_canon_email(to_email),
        subject=(subject or "").strip() or None,
        title_snapshot=title or None,
        service_label=_service_from_link(link) or None,
        photo_url=(offer_effective_photo(off) or "").strip() or None if off else None,
        offer_price=(offer_effective_price(off, default="") or "").strip() or None if off else None,
        ad_url_snapshot=(link or "").strip() or None,
    )
    session.add(row)
    await session.flush()


async def resolve_mailing_reply_context(
    session,
    *,
    user_id: int,
    inbox_email: str,
    subject: str,
    from_email: str = "",
    from_name: str = "",
) -> MailingReplyContext | None:
    """
    Строго лот с сегодняшней/прошлой рассылки с этого inbox.
    Приоритет: offer_email_id → to_email → validated email на лоте → тема/снапшот.
    """
    inbox = _canon_email(inbox_email)
    if not inbox:
        return None

    contact = _canon_email(from_email) if (from_email or "").strip() else ""
    fn = (from_name or "").strip().lower()
    subj_norm = normalized_reply_subject(subject)
    subj_c = _title_compact(subj_norm) if subj_norm else ""
    subj_strong = subject_is_informative(subject)

    rows = (
        await session.execute(
            sa_select(MailingSend)
            .where(MailingSend.user_id == int(user_id))
            .where(func.lower(MailingSend.inbox_email) == inbox)
            .order_by(MailingSend.id.desc())
            .limit(400)
        )
    ).scalars().all()
    if not rows:
        return None

    from services.subject_offer import subjects_same_for_mailing

    contact_oids: set[int] = set()
    if contact:
        contact_oids = await _offer_ids_with_email(session, int(user_id), contact)

    # 0) Тема ответа = тема /send (Kurze Frage: Mac Pro… ↔ Re: Kurze Frage: Mac Pro…)
    subj_rows = [r for r in rows if subjects_same_for_mailing(r.subject or "", subject)]
    if subj_rows:
        narrowed = subj_rows
        if contact:
            by_contact = [
                r
                for r in subj_rows
                if _canon_email(r.to_email or "") == contact
                or int(r.offer_id) in contact_oids
            ]
            if by_contact:
                narrowed = by_contact
        if len(narrowed) == 1:
            row0 = narrowed[0]
            off0 = await _load_offer(session, int(user_id), int(row0.offer_id))
            if off0:
                return _ctx_from_row(row0, off0)

    def _pick_row(candidates: list[MailingSend]) -> MailingSend | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        matched = [r for r in candidates if _snapshot_matches_reply(r, subject)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return matched[0]
        if not subj_strong and len(candidates) == 1:
            return candidates[0]
        return None

    # 1) Журнал по OfferEmail.id (куда ушла рассылка) + from совпадает с валидированным
    if contact:
        oe_ids = (
            await session.execute(
                sa_select(OfferEmail.id)
                .join(Offer, Offer.id == OfferEmail.offer_id)
                .where(Offer.user_id == int(user_id))
                .where(func.lower(OfferEmail.email) == contact)
                .limit(50)
            )
        ).scalars().all()
        if oe_ids:
            by_oe = [r for r in rows if r.offer_email_id and int(r.offer_email_id) in {int(x) for x in oe_ids}]
            picked = _pick_row(by_oe)
            if picked:
                off = await _load_offer(session, int(user_id), int(picked.offer_id))
                if off:
                    return _ctx_from_row(picked, off)

    # 2) to_email == from ответа (или единственная рассылка на этот контакт)
    if contact:
        contact_rows = [r for r in rows if _canon_email(r.to_email or "") == contact]
        subj_contact = [r for r in contact_rows if subjects_same_for_mailing(r.subject or "", subject)]
        if len(subj_contact) == 1:
            rowc = subj_contact[0]
            offc = await _load_offer(session, int(user_id), int(rowc.offer_id))
            if offc:
                return _ctx_from_row(rowc, offc)
        picked = _pick_row(contact_rows)
        if picked:
            off = await _load_offer(session, int(user_id), int(picked.offer_id))
            if off and (
                _snapshot_matches_reply(picked, subject, offer=off)
                or subjects_same_for_mailing(picked.subject or "", subject)
            ):
                return _ctx_from_row(picked, off)
            if off and len(contact_rows) == 1:
                return _ctx_from_row(picked, off)

    # 3) from есть в OfferEmail лота, на который уже писали с этого inbox
    if contact:
        contact_oids = await _offer_ids_with_email(session, int(user_id), contact)
        mailed_oids = {int(r.offer_id) for r in rows}
        relay_oids = contact_oids & mailed_oids
        if relay_oids:
            relay_rows = [r for r in rows if int(r.offer_id) in relay_oids]
            picked = _pick_row(relay_rows)
            if picked:
                off = await _load_offer(session, int(user_id), int(picked.offer_id))
                if off and (
                    _snapshot_matches_reply(picked, subject, offer=off)
                    or len(relay_rows) == 1
                ):
                    return _ctx_from_row(picked, off)

    # 4) Ранжирование (тема / снапшот / имя продавца) — как раньше, но возвращаем строку журнала
    best_row: MailingSend | None = None
    best_rank = -1

    for row in rows:
        rank = 0
        sent_subj = normalized_reply_subject(row.subject or "")
        subj_match = subjects_same_for_mailing(row.subject or "", subject)
        title_match = False
        if subj_c and row.title_snapshot:
            tc = _title_compact(row.title_snapshot)
            title_match = bool(tc and (tc == subj_c or tc in subj_c or subj_c in tc))
        if subj_match:
            rank += 200
        elif title_match:
            rank += 150
        row_to = _canon_email(row.to_email or "")
        if contact and row_to == contact:
            rank += 80
        off = await _load_offer(session, int(user_id), int(row.offer_id))
        if not off:
            continue
        snap = (row.title_snapshot or offer_effective_title(off) or "").strip()
        if snap and subj_norm and not _subject_title_conflicts(subject, snap):
            sc = _title_compact(snap)
            if sc and subj_c and (sc == subj_c or sc in subj_c or subj_c in sc):
                rank += 190
        pn = (off.person_name or "").strip().lower()
        if fn and pn and (_ratio(fn, pn) >= 0.72 or fn in pn or pn in fn):
            rank += 70
        if contact and int(row.offer_id) in contact_oids:
            rank += 100
        if subj_strong:
            if rank < 100:
                continue
        elif rank <= 0:
            continue
        lot_title = offer_effective_title(off)
        if lot_title and _subject_title_conflicts(subject, lot_title):
            if not (title_match or subj_match or _snapshot_matches_reply(row, subject, offer=off)):
                continue
        snap_ok = _snapshot_matches_reply(row, subject, offer=off)
        if subj_strong and not snap_ok and not offer_matches_incoming_subject(off, subject):
            if not (title_match or subj_match):
                continue
        if rank > best_rank:
            best_rank = rank
            best_row = row

    if best_row:
        off = await _load_offer(session, int(user_id), int(best_row.offer_id))
        if off:
            return _ctx_from_row(best_row, off)

    return None


async def find_offer_by_mailing_log(
    session,
    *,
    user_id: int,
    inbox_email: str,
    subject: str,
    from_email: str = "",
    from_name: str = "",
) -> Offer | None:
    ctx = await resolve_mailing_reply_context(
        session,
        user_id=int(user_id),
        inbox_email=inbox_email,
        subject=subject,
        from_email=from_email,
        from_name=from_name,
    )
    return ctx.offer if ctx else None


async def find_offer_by_offer_email_id(
    session,
    *,
    user_id: int,
    offer_email_id: int,
) -> Offer | None:
    """Прямой поиск по OfferEmail.id (если сохранён в письме)."""
    row = (
        await session.execute(
            sa_select(OfferEmail, Offer)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(OfferEmail.id == int(offer_email_id))
            .where(Offer.user_id == int(user_id))
            .limit(1)
        )
    ).first()
    if not row:
        return None
    _oe, off = row
    return off
