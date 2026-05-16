"""Поиск Offer по email, названию, цене, ссылке и полному JSON."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import func, or_ as sa_or, select as sa_select

from models import Offer, OfferEmail
from services.offer_storage import link_key, parse_offer_raw

_PRICE_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


def _ratio(a: str, b: str) -> float:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _norm_subject(subject: str) -> str:
    s = (subject or "").strip()
    if not s:
        return ""
    return re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", s, flags=re.I).strip()


def _price_token(price: str) -> str:
    m = _PRICE_NUM_RE.search((price or "").replace(" ", ""))
    if not m:
        return ""
    return m.group(1).replace(",", ".")


def _canon_email(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e:
        return e
    local, domain = e.split("@", 1)
    local = local.strip()
    domain = domain.strip().lower()
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in ("googlemail.com", "gmail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def score_offer(
    off: Offer,
    *,
    from_email: str = "",
    subject: str = "",
    from_name: str = "",
    body_text: str = "",
    email_hit: bool = False,
) -> float:
    score = 0.0
    subj = _norm_subject(subject)
    fn = (from_name or "").strip()
    body = (body_text or "").strip()
    body_l = body.lower()

    if email_hit:
        score += 120.0

    title = (off.title or "").strip()
    if subj and title:
        score += 90.0 * _ratio(subj, title)
        if subj.lower() in title.lower() or title.lower() in subj.lower():
            score += 15.0

    pname = (off.person_name or "").strip()
    if fn and pname:
        score += 45.0 * _ratio(fn, pname)
        if fn.lower() in pname.lower() or pname.lower() in fn.lower():
            score += 10.0

    price_tok = _price_token(off.price or "")
    if price_tok and price_tok in body.replace(" ", "").replace(",", "."):
        score += 35.0
    if price_tok and subj and price_tok in subj.replace(" ", ""):
        score += 20.0

    link = (off.link or "").strip()
    if link and link in body:
        score += 55.0
    lk = link_key(link)
    if lk and lk in body_l:
        score += 40.0

    raw = parse_offer_raw(getattr(off, "raw_json", None))
    if raw:
        loc = str(raw.get("location") or "").strip()
        if loc and len(loc) >= 3 and loc.lower() in body_l:
            score += 25.0
        raw_title = str(raw.get("item_title") or raw.get("title") or "").strip()
        if subj and raw_title:
            score += 30.0 * _ratio(subj, raw_title)
        raw_name = str(raw.get("item_person_name") or raw.get("person_name") or "").strip()
        if fn and raw_name:
            score += 25.0 * _ratio(fn, raw_name)
        score += _score_raw_json_fields(raw, subj=subj, fn=fn, body_l=body_l)

    return score


_RAW_SKIP_KEYS = frozenset(
    {"validated_emails", "offer_id", "item_photo", "photo", "image", "img", "email"}
)
_RAW_FIELD_WEIGHT: dict[str, float] = {
    "item_title": 28.0,
    "title": 28.0,
    "item_price": 22.0,
    "price": 22.0,
    "item_person_name": 20.0,
    "person_name": 20.0,
    "name": 18.0,
    "item_desc": 18.0,
    "location": 16.0,
    "item_link": 30.0,
    "link": 30.0,
    "person_link": 14.0,
    "phone": 25.0,
    "gender": 8.0,
}


def _score_raw_json_fields(
    raw: dict[str, Any],
    *,
    subj: str,
    fn: str,
    body_l: str,
) -> float:
    """Доп. баллы, если значения из парсера встречаются в письме."""
    hay = f"{subj} {fn} {body_l}".lower()
    extra = 0.0
    seen_vals: set[str] = set()
    for key, val in raw.items():
        if key in _RAW_SKIP_KEYS or val is None:
            continue
        if isinstance(val, (int, float)):
            s = str(val).strip()
        elif isinstance(val, str):
            s = val.strip()
        else:
            continue
        if len(s) < 3:
            continue
        sl = s.lower()
        if sl in seen_vals:
            continue
        seen_vals.add(sl)
        w = _RAW_FIELD_WEIGHT.get(str(key), 10.0)
        if sl in body_l or sl in hay:
            extra += w
            continue
        if key in ("item_link", "link", "person_link"):
            lk = link_key(s)
            if lk and lk in body_l:
                extra += w
    return extra


async def resolve_offer_for_incoming(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str,
    from_name: str,
    body_text: str = "",
) -> tuple[int | None, int | None]:
    """Найти Offer: сначала email, затем скоринг по всем полям."""
    fe_raw = (from_email or "").strip().lower()
    fe_can = _canon_email(fe_raw)

    email_pairs: list[tuple[OfferEmail, Offer]] = []
    q = (
        sa_select(OfferEmail, Offer)
        .join(Offer, Offer.id == OfferEmail.offer_id)
        .where(Offer.user_id == int(user_id))
    )
    conds = []
    if fe_raw:
        conds.append(func.lower(OfferEmail.email) == fe_raw)
    if fe_can and "@" in fe_can:
        local_can, domain_can = fe_can.split("@", 1)
        if domain_can in ("gmail.com", "googlemail.com"):
            conds.append(func.replace(func.lower(OfferEmail.email), ".", "") == fe_can.replace(".", ""))
        if local_can:
            conds.append(func.lower(OfferEmail.email).like(local_can + "@%"))
    if conds:
        email_pairs = (
            await session.execute(q.where(sa_or(*conds)).order_by(Offer.id.desc()).limit(80))
        ).all()

    if not email_pairs and fe_can:
        all_rows = (
            await session.execute(
                sa_select(OfferEmail, Offer)
                .join(Offer, Offer.id == OfferEmail.offer_id)
                .where(Offer.user_id == int(user_id))
                .order_by(Offer.id.desc())
                .limit(1200)
            )
        ).all()
        for oe, off in all_rows:
            if _canon_email((oe.email or "").strip().lower()) == fe_can:
                email_pairs.append((oe, off))
                break

    candidates: dict[int, tuple[Offer, OfferEmail | None, bool]] = {}
    for oe, off in email_pairs:
        candidates[int(off.id)] = (off, oe, True)

    # Всегда добавляем свежие офферы для матча по title/price/link/raw
    recent = (
        await session.execute(
            sa_select(Offer)
            .where(Offer.user_id == int(user_id))
            .order_by(Offer.id.desc())
            .limit(500)
        )
    ).scalars().all()
    for off in recent:
        oid = int(off.id)
        if oid not in candidates:
            candidates[oid] = (off, None, False)

    if not candidates:
        return None, None

    best_offer_id: int | None = None
    best_email_id: int | None = None
    best_score = -1.0

    for off, oe, email_hit in candidates.values():
        sc = score_offer(
            off,
            from_email=from_email,
            subject=subject,
            from_name=from_name,
            body_text=body_text,
            email_hit=email_hit,
        )
        if sc > best_score:
            best_score = sc
            best_offer_id = int(off.id)
            best_email_id = int(oe.id) if oe else None

    # Порог: email — всегда ок; без email — нужен сильный матч по полям
    min_score = 45.0 if not email_pairs else 35.0
    if best_offer_id is not None and best_score >= min_score:
        return best_offer_id, best_email_id
    return None, None
