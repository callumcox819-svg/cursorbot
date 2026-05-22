"""Журнал рассылки: offer_id + тема + ящик → точный лот при ответе продавца."""

from __future__ import annotations

from sqlalchemy import func, select as sa_select

from models import MailingSend, Offer
from services.offer_matching import (
    _canon_email,
    _ratio,
    normalized_reply_subject,
    offer_matches_incoming_subject,
    subject_is_informative,
)
from services.offer_matching import _subject_title_conflicts
from services.offer_storage import _title_compact, offer_effective_title


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
) -> None:
    row = MailingSend(
        user_id=int(user_id),
        offer_id=int(offer_id),
        offer_email_id=int(offer_email_id) if offer_email_id else None,
        inbox_email=_canon_email(inbox_email),
        to_email=_canon_email(to_email),
        subject=(subject or "").strip() or None,
        title_snapshot=(title_snapshot or "").strip() or None,
    )
    session.add(row)


async def find_offer_by_mailing_log(
    session,
    *,
    user_id: int,
    inbox_email: str,
    subject: str,
    from_email: str = "",
    from_name: str = "",
) -> Offer | None:
    """
    Лот по факту рассылки: с какого ящика, какая тема, какой offer_id ушёл на to_email.
    Работает, когда продавец отвечает с другого Gmail, чем валидированный адрес.
    """
    inbox = _canon_email(inbox_email)
    if not inbox:
        return None

    subj_norm = normalized_reply_subject(subject)
    subj_c = _title_compact(subj_norm) if subj_norm else ""
    contact = _canon_email(from_email) if (from_email or "").strip() else ""
    fn = (from_name or "").strip().lower()
    subj_strong = subject_is_informative(subject)
    rows = (
        await session.execute(
            sa_select(MailingSend)
            .where(MailingSend.user_id == int(user_id))
            .where(func.lower(MailingSend.inbox_email) == inbox)
            .order_by(MailingSend.id.desc())
            .limit(300)
        )
    ).scalars().all()

    # Писали на этот же адрес, с которого пришёл ответ — берём offer_id из журнала /send.
    if contact:
        contact_rows = [r for r in rows if _canon_email(r.to_email or "") == contact]
        if len(contact_rows) == 1:
            row = contact_rows[0]
            off = (
                await session.execute(
                    sa_select(Offer)
                    .where(Offer.id == int(row.offer_id))
                    .where(Offer.user_id == int(user_id))
                    .limit(1)
                )
            ).scalars().first()
            if off and offer_effective_link(off):
                snap = (row.title_snapshot or offer_effective_title(off) or "").strip()
                if not snap or not _subject_title_conflicts(subject, snap):
                    return off
                subj_c = _title_compact(subj_norm) if subj_norm else ""
                sc = _title_compact(snap)
                if sc and subj_c and (sc == subj_c or sc in subj_c or subj_c in sc):
                    return off
        elif len(contact_rows) > 1:
            subj_c = _title_compact(subj_norm) if subj_norm else ""
            for row in contact_rows:
                snap = (row.title_snapshot or "").strip()
                if not snap or not subj_norm:
                    continue
                sc = _title_compact(snap)
                if sc and subj_c and (sc == subj_c or sc in subj_c or subj_c in sc):
                    off = (
                        await session.execute(
                            sa_select(Offer)
                            .where(Offer.id == int(row.offer_id))
                            .where(Offer.user_id == int(user_id))
                            .limit(1)
                        )
                    ).scalars().first()
                    if off and offer_effective_link(off):
                        return off

    best: Offer | None = None
    best_rank = -1

    for row in rows:
        rank = 0
        sent_subj = normalized_reply_subject(row.subject or "")
        subj_match = bool(subj_norm and sent_subj and sent_subj == subj_norm)
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
        off = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.id == int(row.offer_id))
                .where(Offer.user_id == int(user_id))
                .limit(1)
            )
        ).scalars().first()
        if not off:
            continue
        snap = (row.title_snapshot or "").strip()
        if snap and subj_norm and not _subject_title_conflicts(subject, snap):
            sc = _title_compact(snap)
            if sc and (sc == subj_c or sc in subj_c or subj_c in sc):
                rank += 190
        pn = (off.person_name or "").strip().lower()
        if fn and pn and (_ratio(fn, pn) >= 0.72 or fn in pn or pn in fn):
            rank += 70
        if subj_strong:
            if rank < 120:
                continue
        elif rank <= 0:
            continue
        lot_title = offer_effective_title(off)
        if lot_title and _subject_title_conflicts(subject, lot_title):
            continue
        snap_ok = bool(
            snap
            and subj_norm
            and not _subject_title_conflicts(subject, snap)
            and _title_compact(snap)
            and (
                _title_compact(snap) == subj_c
                or _title_compact(snap) in subj_c
                or subj_c in _title_compact(snap)
            )
        )
        if subj_strong and not snap_ok and not offer_matches_incoming_subject(off, subject):
            # Tutti/relay: ответ с другого From, но тема/снапшот рассылки совпали — берём лот.
            if not (title_match or subj_match):
                continue
        if rank > best_rank:
            best_rank = rank
            best = off

    return best


async def find_offer_by_offer_email_id(
    session,
    *,
    user_id: int,
    offer_email_id: int,
) -> Offer | None:
    """Прямой поиск по OfferEmail.id (если сохранён в письме)."""
    from models import OfferEmail

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
