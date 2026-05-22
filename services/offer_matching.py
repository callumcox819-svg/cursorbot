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
    for ch in ("\u2013", "\u2014", "\u2012", "–", "—", "−"):
        s = s.replace(ch, "-")
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


def canon_seller_email(email: str) -> str:
    return _canon_email(email)


_SUBJECT_STOP = frozenset(
    {
        "re",
        "aw",
        "fw",
        "fwd",
        "the",
        "und",
        "der",
        "die",
        "das",
        "for",
        "von",
        "from",
    }
)

# Общие слова в теме Re: — не считаем совпадением лота (Porte bébé ≠ Baignoire bébé).
_SUBJECT_WEAK = frozenset(
    {
        "bebe",
        "baby",
        "fb",
        "neu",
        "neuf",
        "bon",
        "etat",
        "sehr",
        "gut",
        "avec",
        "pour",
        "sale",
        "sold",
        "neu",
        "chf",
        "eur",
    }
)

# Место / способ получения в OFFER-теме — не требуем в названии лота (Bulach, Abholung).
_SUBJECT_LOCATION = frozenset(
    {
        "abholung",
        "abholen",
        "pickup",
        "collection",
        "bulach",
        "zur",
        "zum",
        "von",
        "aus",
        "bei",
        "ort",
        "stadt",
        "region",
        "kanton",
        "verfuegbar",
        "available",
        "disponible",
        "noch",
        "frage",
        "anfrage",
        "kurze",
    }
)

# Разные категории товара в теме и в названии лота (Sofa vs Télévision samsung).
_PRODUCT_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"sofa", "couch", "sectional", "divan", "canape", "canapee", "ecksofa", "sessel"}),
    frozenset({"tv", "fernseher", "television", "televisor", "monitor", "bildschirm"}),
    frozenset({"bike", "fahrrad", "velo", "bicycle", "ebike", "e-bike"}),
    frozenset({"table", "tisch", "desk", "schreibtisch"}),
    frozenset({"chair", "stuhl", "stuhle", "stuehle", "chairs"}),
    frozenset({"fridge", "kuehlschrank", "kühlschrank", "refrigerator", "gefrierschrank"}),
    frozenset({"washer", "waschmaschine", "washing", "trockner", "dryer"}),
    frozenset({"phone", "iphone", "samsung", "handy", "smartphone"}),
    frozenset({"porte", "poussette", "landau", "poussettes"}),
    frozenset({"baignoire", "baignoires", "bain"}),
)


def _product_group_hits(text: str) -> set[int]:
    folded = _fold_match_text(text)
    if not folded:
        return set()
    toks = set(_subject_tokens(folded))
    hits: set[int] = set()
    for i, group in enumerate(_PRODUCT_GROUPS):
        if any(g in folded or g in toks for g in group):
            hits.add(i)
    return hits


_SUBJECT_WORD_RE = re.compile(r"[^\W\d_]{3,}", flags=re.UNICODE)
_SUBJECT_NUM_RE = re.compile(r"\d{2,}")


def _fold_de(s: str) -> str:
    s = (s or "").lower()
    for src, dst in (
        ("ä", "ae"),
        ("ö", "oe"),
        ("ü", "ue"),
        ("ß", "ss"),
        ("é", "e"),
        ("è", "e"),
        ("ê", "e"),
        ("ë", "e"),
        ("à", "a"),
        ("â", "a"),
        ("ô", "o"),
        ("û", "u"),
        ("ç", "c"),
    ):
        s = s.replace(src, dst)
    return s


def product_match_tokens(subj: str) -> list[str]:
    """Токены товара из OFFER-части темы (без локации и служебных слов)."""
    core = offer_title_from_mail_subject(subj) or subj
    base = _fold_match_text(core)
    return [
        t
        for t in _subject_tokens(base)
        if len(t) >= 4 and t not in _SUBJECT_WEAK and t not in _SUBJECT_LOCATION
    ]


def _token_in_folded_hay(tok: str, hay: str, *, ratio_min: float = 0.86) -> bool:
    """Токен в строке; kalax ≈ kallax."""
    t = (tok or "").strip().lower()
    h = (hay or "").strip().lower()
    if not t or not h:
        return False
    if t in h:
        return True
    if len(t) < 4:
        return False
    for w in re.findall(r"[^\W\d_]{3,}", h, flags=re.UNICODE):
        if w == t or (len(w) >= 4 and SequenceMatcher(None, t, w).ratio() >= ratio_min):
            return True
    return False


def _product_token_hits(tokens: list[str], hay: str) -> int:
    if not tokens or not hay:
        return 0
    return sum(1 for t in tokens if _token_in_folded_hay(t, hay))


def _subject_distinct_tokens(subj: str) -> list[str]:
    """Слова только из OFFER-названия в теме (товар, не место)."""
    return product_match_tokens(subj)


def _fold_match_text(s: str) -> str:
    return _fold_de(_norm_subject(s))


def _subject_tokens(subj: str) -> list[str]:
    base = _fold_de(_norm_subject(subj))
    parts = _SUBJECT_WORD_RE.findall(base)
    nums = _SUBJECT_NUM_RE.findall(base)
    out = [p for p in parts if p not in _SUBJECT_STOP]
    for n in nums:
        if n not in out:
            out.append(n)
    return out


def offer_title_from_mail_subject(subject: str) -> str:
    """Только подстановка OFFER в теме (название товара), не служебные слова шаблона."""
    from services.subject_offer import offer_title_from_mail_subject as _core

    return _core(subject)


def _subject_significant_tokens(subj: str) -> list[str]:
    """Токены только из OFFER-части темы (название товара)."""
    return product_match_tokens(subj)


async def resolve_offer_by_subject_tokens(
    session,
    *,
    user_id: int,
    subject: str,
    candidate_offers: list[Offer] | None = None,
) -> Offer | None:
    """Фолбэк: токены только из OFFER-названия в теме (≥2 слова или 1 длинное)."""
    from services.offer_storage import offer_effective_title

    core = offer_title_from_mail_subject(subject)
    if not core:
        return None
    toks = _subject_significant_tokens(subject)
    if not toks:
        return None

    offers = candidate_offers
    if offers is None:
        offers = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.user_id == int(user_id))
                .order_by(Offer.id.desc())
                .limit(800)
            )
        ).scalars().all()

    best: Offer | None = None
    best_hits = 0
    for off in offers:
        title = _fold_match_text(offer_effective_title(off))
        if not title:
            continue
        if _subject_title_conflicts(core, offer_effective_title(off)):
            continue
        hits = _product_token_hits(toks, title)
        need = 2
        if len(toks) == 1 and len(toks[0]) >= 7:
            need = 1
        if hits >= need and hits > best_hits:
            best_hits = hits
            best = off
    return best


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


async def _offer_email_id_for_offer(session, user_id: int, offer_id: int) -> int | None:
    row = (
        await session.execute(
            sa_select(OfferEmail.id)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == int(user_id))
            .where(OfferEmail.offer_id == int(offer_id))
            .order_by(OfferEmail.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row) if row else None


async def resolve_offer_for_incoming(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str,
    from_name: str,
    body_text: str = "",
) -> tuple[int | None, int | None]:
    """Найти Offer: при информативной теме — сначала по теме, затем email + скоринг."""
    subj_strong = subject_is_informative(subject)

    if subj_strong:
        off_g = await resolve_best_offer_by_subject_global(
            session,
            user_id=int(user_id),
            subject=subject,
            from_email=from_email,
            from_name=from_name,
            body_text=body_text,
        )
        if off_g and offer_matches_incoming_subject(off_g, subject):
            oe_id = await _offer_email_id_for_offer(session, int(user_id), int(off_g.id))
            return int(off_g.id), oe_id

        off_e = await resolve_best_offer_by_subject(
            session,
            user_id=int(user_id),
            from_email=from_email,
            subject=subject,
            from_name=from_name,
            body_text=body_text,
        )
        if off_e and offer_matches_incoming_subject(off_e, subject):
            oe_id = await _offer_email_id_for_offer(session, int(user_id), int(off_e.id))
            return int(off_e.id), oe_id

    if subj_strong:
        return None, None

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
        if subj_strong and email_hit:
            sm_pre = subject_match_score(subject, off)
            if sm_pre < 45.0:
                continue
        sc = score_offer(
            off,
            from_email=from_email,
            subject=subject,
            from_name=from_name,
            body_text=body_text,
            email_hit=email_hit,
        )
        if subj_strong and email_hit:
            sm = subject_match_score(subject, off)
            if sm >= 70.0:
                sc += 40.0
        if sc > best_score:
            best_score = sc
            best_offer_id = int(off.id)
            best_email_id = int(oe.id) if oe else None

    # Порог: email — всегда ок; без email — нужен сильный матч по полям
    min_score = 45.0 if not email_pairs else 35.0
    if best_offer_id is not None and best_score >= min_score:
        return best_offer_id, best_email_id
    return None, None


def _subject_title_conflicts(subj: str, title: str) -> bool:
    """Явное противоречие OFFER-части темы и названия оффера."""
    core = offer_title_from_mail_subject(subj) or subj
    subj = _fold_match_text(core)
    title_l = _fold_match_text(title)
    if not subj or not title_l:
        return False

    subj_num = re.match(r"^(\d+)\b", subj)
    title_num = re.match(r"^(\d+)\b", title_l) or re.search(r"\b(\d+)\s*st", title_l)
    if subj_num and title_num and subj_num.group(1) != title_num.group(1):
        return True

    free_words = ("gratis", "free", "kostenlos", "gratuit")
    subj_free = any(w in subj for w in free_words)
    title_priced = bool(re.search(r"\d+\s*\.?\s*-", title_l)) or "chf" in title_l or "eur" in title_l
    if subj_free and title_priced and not any(w in title_l for w in free_words):
        return True

    subj_toks = set(_subject_tokens(subj))
    title_toks = set(_subject_tokens(title_l))
    # Re: название лота в теме (+ место Abholung) — не конфликт.
    title_product = [t for t in title_toks if len(t) >= 4 and t not in _SUBJECT_WEAK]
    if len(title_product) >= 2 and all(_token_in_folded_hay(t, subj) for t in title_product):
        return False

    distinct = product_match_tokens(subj)
    if distinct:
        matched = _product_token_hits(distinct, title_l)
        need = 2 if len(distinct) >= 2 else 1
        if matched >= need:
            return False
        if matched < need:
            return True
        title_distinct = product_match_tokens(title_l)
        if title_distinct:
            back = _product_token_hits(title_distinct, subj)
            back_need = 2 if len(title_distinct) >= 2 else 1
            if back < back_need:
                return True

    sig_min = 5
    significant = [
        t
        for t in _subject_tokens(subj)
        if len(t) >= sig_min
        and t not in ("stuhle", "stuhl", "chair", "chairs")
        and t not in _SUBJECT_LOCATION
    ]
    title_sig = [
        t
        for t in _subject_tokens(title_l)
        if len(t) >= sig_min and t not in ("stuhle", "stuhl", "chair", "chairs")
    ]
    if title_sig:
        extra_in_title = sum(1 for t in title_sig if not _token_in_folded_hay(t, subj))
        if extra_in_title >= 2:
            return True
    if significant:
        missing = sum(1 for t in significant if not _token_in_folded_hay(t, title_l))
        if missing >= 3:
            return True
        if (
            missing >= 2
            and len(title_toks) >= 2
            and not title_toks <= subj_toks
            and "wohnzimmer" in subj
            and "wohnzimmer" not in title_l
        ):
            return True

    subj_groups = _product_group_hits(subj)
    title_groups = _product_group_hits(title_l)
    if subj_groups and title_groups and not (subj_groups & title_groups):
        return True

    # Re: Sofa — один короткий токен-товар, в лоте другая категория.
    short_subj = [t for t in _subject_tokens(subj) if 3 <= len(t) <= 5]
    if len(short_subj) == 1 and title_sig and short_subj[0] not in title_l:
        if any(t not in subj for t in title_sig):
            return True
    return False


def normalized_reply_subject(subject: str) -> str:
    return _norm_subject(subject)


def offer_matches_incoming_subject(off: Offer, subject: str, *, min_score: float = 45.0) -> bool:
    if not subject_is_informative(subject):
        return True
    from services.offer_storage import offer_effective_title

    title = offer_effective_title(off)
    if title and _subject_title_conflicts(subject, title):
        return False
    return subject_match_score(subject, off) >= float(min_score)


def offer_acceptable_for_subject(off: Offer | None, subject: str) -> bool:
    """Лот можно показывать в карточке / GAG только если тема письма совпадает с названием."""
    if not off:
        return False
    from services.offer_storage import offer_effective_title

    if not subject_is_informative(subject):
        return True
    title = offer_effective_title(off)
    if title and _subject_title_conflicts(subject, title):
        return False
    if not title:
        return True
    return offer_matches_incoming_subject(off, subject, min_score=38.0)


def gag_title_for_mail(*, offer: Offer | None, subject: str) -> str:
    """Название для GAG: OFFER-часть темы важнее чужого лота в БД."""
    subj = offer_title_from_mail_subject(subject) or (subject or "").strip()
    if not offer or not offer_acceptable_for_subject(offer, subject):
        return subj
    from services.offer_storage import offer_effective_title

    return offer_effective_title(offer) or subj


def subject_is_informative(subject: str) -> bool:
    """Информативна, если в теме есть подстановка OFFER (название товара)."""
    core = offer_title_from_mail_subject(subject)
    if core and len(core.replace(" ", "")) >= 5:
        return True
    subj = _norm_subject(subject)
    if not subj:
        return False
    if len(subj) >= 8:
        return True
    toks = _subject_tokens(subj)
    if len(toks) >= 2:
        return True
    if len(toks) == 1 and len(toks[0]) >= 3:
        return True
    if _product_group_hits(subj):
        return True
    return len(subj) >= 4


def subject_token_hits(subject: str, off: Offer) -> int:
    from services.offer_storage import offer_effective_title

    core = offer_title_from_mail_subject(subject)
    if not core:
        return 0
    title_l = _fold_match_text(offer_effective_title(off))
    if not title_l:
        return 0
    return _product_token_hits(product_match_tokens(subject), title_l)


def subject_match_score(subject: str, off: Offer) -> float:
    """Матч OFFER-названия из темы к title лота."""
    from services.offer_storage import offer_effective_title

    core = offer_title_from_mail_subject(subject)
    if not core:
        return 0.0
    subj = _fold_match_text(core)
    if len(subj) < 4:
        return 0.0
    raw_title = offer_effective_title(off)
    if not raw_title:
        return 0.0
    if _subject_title_conflicts(core, raw_title):
        return 0.0

    title = _fold_match_text(raw_title)

    score = 75.0 * _ratio(subj, title)
    if subj in title or title in subj:
        score += 55.0

    subj_l = subj
    title_l = title
    tok_hits = 0
    for tok in product_match_tokens(subject):
        if _token_in_folded_hay(tok, title_l):
            tok_hits += 1
            score += 24.0
    if tok_hits >= 2:
        score += 25.0 + tok_hits * 12.0
    if tok_hits >= 3:
        score += 40.0

    wants_set = any(w in subj_l for w in ("komplette", "complet", "complete", "set "))
    if wants_set:
        if any(w in title_l for w in ("komplette", "complet", "complete", "set")):
            score += 45.0
        if any(w in title_l for w in ("sticker", "valverde", "extra sticker")) and not any(
            w in title_l for w in ("komplette", "complet", "set")
        ):
            score -= 50.0

    return score


def offer_has_validated_email_in_raw(off: Offer, email: str) -> bool:
    """Только validated_email / validated_emails из JSON после валидации (не пустой email парсера)."""
    from services.offer_storage import parse_offer_raw

    fe = _canon_email(email)
    if not fe:
        return False
    raw = parse_offer_raw(getattr(off, "raw_json", None))
    ve = _canon_email(str(raw.get("validated_email") or ""))
    if ve and ve == fe:
        return True
    for em in raw.get("validated_emails") or []:
        if _canon_email(str(em or "")) == fe:
            return True
    return False


def offer_has_contact_email(off: Offer, email: str) -> bool:
    """Валидированная почта в raw_json (validated_*) — для входящих и GAG."""
    return offer_has_validated_email_in_raw(off, email)


async def list_offers_for_incoming_contact(
    session,
    *,
    user_id: int,
    from_email: str,
    from_name: str = "",
) -> list[Offer]:
    """Лоты продавца: OfferEmail + validated_emails в raw_json (как после импорта/валидации)."""
    fe_can = _canon_email(from_email)
    seen: set[int] = set()
    out: list[Offer] = []

    def _add(off: Offer | None) -> None:
        if not off:
            return
        oid = int(off.id)
        if oid in seen:
            return
        seen.add(oid)
        out.append(off)

    for off in await list_offers_for_seller_email(
        session, user_id=int(user_id), from_email=from_email
    ):
        _add(off)

    if fe_can:
        recent = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.user_id == int(user_id))
                .order_by(Offer.id.desc())
                .limit(2500)
            )
        ).scalars().all()
        for off in recent:
            if offer_has_validated_email_in_raw(off, fe_can):
                _add(off)

    if (from_name or "").strip() and not fe_can:
        for off in await list_offers_for_seller_name(
            session, user_id=int(user_id), from_name=from_name
        ):
            _add(off)

    return out


async def list_offers_for_seller_email(
    session,
    *,
    user_id: int,
    from_email: str,
) -> list[Offer]:
    fe_raw = (from_email or "").strip().lower()
    fe_can = _canon_email(fe_raw)
    if not fe_can:
        return []

    conds = []
    if fe_raw:
        conds.append(func.lower(OfferEmail.email) == fe_raw)
    if fe_can and "@" in fe_can:
        conds.append(func.lower(OfferEmail.email) == fe_can)
        local_can, domain_can = fe_can.split("@", 1)
        if domain_can in ("gmail.com", "googlemail.com"):
            conds.append(
                func.replace(func.lower(OfferEmail.email), ".", "") == fe_can.replace(".", "")
            )

    if not conds:
        return []

    rows = (
        await session.execute(
            sa_select(Offer)
            .join(OfferEmail, OfferEmail.offer_id == Offer.id)
            .where(Offer.user_id == int(user_id))
            .where(sa_or(*conds))
            .order_by(Offer.id.desc())
        )
    ).scalars().all()

    seen: set[int] = set()
    out: list[Offer] = []
    for off in rows:
        oid = int(off.id)
        if oid in seen:
            continue
        seen.add(oid)
        out.append(off)

    if out or not fe_can:
        return out

    # Fallback: тот же продавец, другой домен / точки в local-part (gmail vs hotmail).
    try:
        email_rows = (
            await session.execute(
                sa_select(OfferEmail.email, Offer.id)
                .join(Offer, Offer.id == OfferEmail.offer_id)
                .where(Offer.user_id == int(user_id))
                .order_by(OfferEmail.id.desc())
                .limit(2000)
            )
        ).all()
        oid_to_off: dict[int, Offer] = {}
        for em, oid in email_rows:
            if _canon_email(em or "") != fe_can:
                continue
            if int(oid) in seen:
                continue
            seen.add(int(oid))
        if seen:
            offs = (
                await session.execute(
                    sa_select(Offer).where(Offer.id.in_(list(seen))).order_by(Offer.id.desc())
                )
            ).scalars().all()
            out.extend(offs)
    except Exception:
        pass
    return out


async def list_offers_for_seller_name(
    session,
    *,
    user_id: int,
    from_name: str,
) -> list[Offer]:
    """Офферы по имени продавца из парсера (если email ответа ≠ угаданному при валидации)."""
    fn = (from_name or "").strip().lower()
    if len(fn) < 4:
        return []
    recent = (
        await session.execute(
            sa_select(Offer).where(Offer.user_id == int(user_id)).order_by(Offer.id.desc()).limit(400)
        )
    ).scalars().all()
    out: list[Offer] = []
    for off in recent:
        pn = (off.person_name or "").strip().lower()
        if not pn:
            raw = parse_offer_raw(getattr(off, "raw_json", None))
            pn = str(raw.get("item_person_name") or raw.get("person_name") or "").strip().lower()
        if not pn:
            continue
        if fn in pn or pn in fn or _ratio(fn, pn) >= 0.72:
            out.append(off)
    return out


def _pick_best_offer_by_subject_scores(
    offers: list[Offer],
    *,
    subject: str,
    from_name: str = "",
    body_text: str = "",
    min_score: float,
) -> Offer | None:
    if not offers or not subject_is_informative(subject):
        return None

    best: Offer | None = None
    best_sc = -1.0
    for off in offers:
        sc = subject_match_score(subject, off)
        sc += (
            score_offer(
                off,
                subject=subject,
                from_name=from_name,
                body_text=body_text,
                email_hit=False,
            )
            * 0.3
        )
        if sc > best_sc:
            best_sc = sc
            best = off

    if best is None:
        return None
    from services.offer_storage import offer_effective_title

    if _subject_title_conflicts(subject, offer_effective_title(best)):
        return None
    if best_sc >= min_score:
        return best
    if subject_token_hits(subject, best) >= 1 and best_sc >= max(28.0, min_score - 18.0):
        return best
    if subject_token_hits(subject, best) >= 3 and best_sc >= 48.0:
        return best
    return None


async def resolve_best_offer_by_subject(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str,
    from_name: str = "",
    body_text: str = "",
) -> Offer | None:
    offers = await list_offers_for_seller_email(session, user_id=int(user_id), from_email=from_email)
    if not offers:
        return None

    multi = len(offers) > 1
    picked = _pick_best_offer_by_subject_scores(
        offers,
        subject=subject,
        from_name=from_name,
        body_text=body_text,
        min_score=42.0 if multi else 36.0,
    )
    if picked:
        return picked
    return None


async def resolve_best_offer_by_subject_global(
    session,
    *,
    user_id: int,
    subject: str,
    from_email: str = "",
    from_name: str = "",
    body_text: str = "",
) -> Offer | None:
    """Если email привязан к другому лоту — ищем оффер по теме среди всех объявлений пользователя."""
    if not subject_is_informative(subject):
        return None

    seller_offs = await list_offers_for_seller_email(
        session, user_id=int(user_id), from_email=from_email
    )
    if not seller_offs and from_name and not _canon_email(from_email):
        seller_offs = await list_offers_for_seller_name(
            session, user_id=int(user_id), from_name=from_name
        )
    if seller_offs:
        picked = _pick_best_offer_by_subject_scores(
            seller_offs,
            subject=subject,
            from_name=from_name,
            body_text=body_text,
            min_score=34.0,
        )
        if picked:
            return picked

    recent = (
        await session.execute(
            sa_select(Offer)
            .where(Offer.user_id == int(user_id))
            .order_by(Offer.id.desc())
            .limit(800)
        )
    ).scalars().all()
    picked = _pick_best_offer_by_subject_scores(
        list(recent),
        subject=subject,
        from_name=from_name,
        body_text=body_text,
        min_score=46.0,
    )
    if picked:
        return picked
    return None


async def resolve_offer_for_incoming_mail(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str,
    from_name: str = "",
    body_text: str = "",
    stored_offer_id: int | None = None,
    inbox_email: str | None = None,
    offer_email_id: int | None = None,
) -> Offer | None:
    """
    Оффер под ЭТО письмо: журнал /send → OfferEmail с этим from_email → тема только среди этих лотов.
    """
    subj = (subject or "").strip()
    fe_can = _canon_email(from_email)
    from services.offer_storage import _offer_ids_with_email

    email_offer_ids: set[int] = (
        await _offer_ids_with_email(session, int(user_id), fe_can) if fe_can else set()
    )

    def _incoming_offer_ok(off: Offer | None, *, from_mailing_log: bool = False) -> bool:
        if not off:
            return False
        from services.offer_storage import offer_effective_title

        title = offer_effective_title(off)
        if title and _subject_title_conflicts(subj, title):
            return False
        if from_mailing_log:
            return True
        if subject_is_informative(subj) and not offer_matches_incoming_subject(off, subj):
            return False
        if fe_can and not from_mailing_log:
            if int(off.id) in email_offer_ids:
                return True
            if offer_has_validated_email_in_raw(off, from_email):
                return True
            return False
        return True

    if offer_email_id:
        from services.mailing_send_log import find_offer_by_offer_email_id

        off_oe = await find_offer_by_offer_email_id(
            session, user_id=int(user_id), offer_email_id=int(offer_email_id)
        )
        if _incoming_offer_ok(off_oe):
            return off_oe

    if (inbox_email or "").strip():
        from services.mailing_send_log import find_offer_by_mailing_log

        off_log = await find_offer_by_mailing_log(
            session,
            user_id=int(user_id),
            inbox_email=inbox_email or "",
            subject=subj,
            from_email=from_email,
            from_name=from_name,
        )
        if _incoming_offer_ok(off_log, from_mailing_log=True):
            return off_log

    if fe_can:
        from services.offer_storage import pick_offer_for_incoming_reply

        off_db = await pick_offer_for_incoming_reply(
            session,
            user_id=int(user_id),
            from_email=from_email,
            subject=subj,
            from_name=from_name,
            inbox_email=inbox_email,
        )
        if off_db:
            return off_db

    def _pinned_incoming_ok(off: Offer | None) -> bool:
        if not off:
            return False
        from services.offer_storage import offer_effective_title

        title = offer_effective_title(off)
        if title and subject_is_informative(subj) and _subject_title_conflicts(subj, title):
            return False
        return True

    if stored_offer_id:
        off = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.id == int(stored_offer_id))
                .where(Offer.user_id == int(user_id))
                .limit(1)
            )
        ).scalars().first()
        if off and _pinned_incoming_ok(off) and _incoming_offer_ok(off):
            return off

    if subject_is_informative(subj):
        from services.offer_storage import find_offer_by_incoming_subject, offer_effective_title

        off_db = await find_offer_by_incoming_subject(
            session,
            user_id=int(user_id),
            subject=subj,
            from_name=from_name,
            from_email=from_email,
        )
        if off_db and _incoming_offer_ok(off_db):
            return off_db

        off = await resolve_best_offer_by_subject(
            session,
            user_id=int(user_id),
            from_email=from_email,
            subject=subj,
            from_name=from_name,
            body_text=body_text,
        )
        if off and _incoming_offer_ok(off):
            return off
        if not fe_can:
            off = await resolve_best_offer_by_subject_global(
                session,
                user_id=int(user_id),
                subject=subj,
                from_email=from_email,
                from_name=from_name,
                body_text=body_text,
            )
            if off and _incoming_offer_ok(off):
                return off

        seller_offers = await list_offers_for_seller_email(
            session, user_id=int(user_id), from_email=from_email
        )
        if fe_can:
            seller_offers = [
                o
                for o in seller_offers
                if int(o.id) in email_offer_ids
                or offer_has_validated_email_in_raw(o, from_email)
            ]
        if len(seller_offers) > 1:
            picked = _pick_best_offer_by_subject_scores(
                seller_offers,
                subject=subj,
                from_name=from_name,
                body_text=body_text,
                min_score=32.0,
            )
            if picked and _incoming_offer_ok(picked):
                return picked
        if len(seller_offers) == 1:
            only = seller_offers[0]
            if offer_has_validated_email_in_raw(only, from_email) or int(only.id) in email_offer_ids:
                return only
            title = offer_effective_title(only)
            if title and not _subject_title_conflicts(subj, title) and _incoming_offer_ok(only):
                return only

        off_tok = await resolve_offer_by_subject_tokens(
            session,
            user_id=int(user_id),
            subject=subj,
            candidate_offers=seller_offers or None,
        )
        if off_tok and _incoming_offer_ok(off_tok):
            return off_tok
        off_tok_g = await resolve_offer_by_subject_tokens(
            session, user_id=int(user_id), subject=subj
        )
        if off_tok_g:
            return off_tok_g

        if not fe_can:
            from services.offer_storage import find_offer_by_subject_aggressive

            off_agg = await find_offer_by_subject_aggressive(
                session,
                user_id=int(user_id),
                subject=subj,
                from_name=from_name,
            )
            if off_agg and _incoming_offer_ok(off_agg):
                return off_agg

        from services.offer_storage import find_offer_by_incoming_signals

        off_sig = await find_offer_by_incoming_signals(
            session,
            user_id=int(user_id),
            from_email=from_email,
            subject=subj,
            from_name=from_name,
            body_text=body_text,
        )
        if _incoming_offer_ok(off_sig):
            return off_sig

        from services.offer_storage import find_offer_by_core_subject_title

        off_core = await find_offer_by_core_subject_title(
            session, user_id=int(user_id), subject=subj
        )
        if off_core:
            return off_core

        return None

    if fe_can and not email_offer_ids:
        recent_chk = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.user_id == int(user_id))
                .order_by(Offer.id.desc())
                .limit(2500)
            )
        ).scalars().all()
        if not any(offer_has_validated_email_in_raw(o, from_email) for o in recent_chk):
            return None

    oid, _ = await resolve_offer_for_incoming(
        session,
        user_id=int(user_id),
        from_email=from_email,
        subject=subj,
        from_name=from_name,
        body_text=body_text,
    )
    if not oid:
        return None
    off = (
        await session.execute(
            sa_select(Offer).where(Offer.id == int(oid)).where(Offer.user_id == int(user_id)).limit(1)
        )
    ).scalars().first()
    if off and _incoming_offer_ok(off) and offer_matches_incoming_subject(off, subj):
        return off
    return None


async def offer_link_for_seller(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str = "",
) -> str | None:
    """Offer.link по email; при информативной теме — матч по теме или один лот продавца."""
    from services.offer_storage import offer_effective_link

    if subject_is_informative(subject):
        off = await resolve_best_offer_by_subject(
            session,
            user_id=int(user_id),
            from_email=from_email,
            subject=subject,
        )
        if off and offer_effective_link(off):
            return offer_effective_link(off)
        off = await resolve_best_offer_by_subject_global(
            session,
            user_id=int(user_id),
            subject=subject,
            from_email=from_email,
        )
        if off and offer_effective_link(off):
            return offer_effective_link(off)
        seller_offers = await list_offers_for_seller_email(
            session, user_id=int(user_id), from_email=from_email
        )
        if len(seller_offers) > 1:
            picked = _pick_best_offer_by_subject_scores(
                seller_offers, subject=subject, min_score=32.0
            )
            if picked and offer_effective_link(picked):
                return offer_effective_link(picked)
        if len(seller_offers) == 1 and offer_effective_link(seller_offers[0]):
            from services.offer_storage import offer_effective_title

            title = offer_effective_title(seller_offers[0])
            if not title or not _subject_title_conflicts(subject, title):
                return offer_effective_link(seller_offers[0])

    if not user_id or not from_email or "@" not in from_email:
        return None

    fe_can = _canon_email(from_email)
    try:
        rows = (
            await session.execute(
                sa_select(OfferEmail.email, Offer.link)
                .select_from(OfferEmail)
                .join(Offer, Offer.id == OfferEmail.offer_id)
                .where(Offer.user_id == int(user_id))
                .where(Offer.link.is_not(None))
                .order_by(OfferEmail.id.desc())
                .limit(2000)
            )
        ).all()
        for em, lk in rows:
            if lk and fe_can and _canon_email(em or "") == fe_can:
                return str(lk).strip()
    except Exception:
        pass
    return None


async def resolve_offer_for_aqua_link(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str,
    from_name: str = "",
    body_text: str = "",
    resolved_offer_id: int | None = None,
    resolved_offer_email_id: int | None = None,
    ad_url: str | None = None,
    inbox_email: str | None = None,
    product_title: str | None = None,
) -> tuple[Offer | None, str]:
    """Оффер + ad_url для «Создать ссылку» (AQUA/GAG) — без битых kwargs в global-поиске."""
    from services.incoming_mail_worker import resolve_offer_for_mail_card
    from services.offer_storage import find_offer_by_link, resolve_offer_from_saved_context

    from services.subject_offer import offer_title_from_mail_subject

    title_hint = (
        (product_title or "").strip()
        or offer_title_from_mail_subject(subject)
        or ""
    ).strip()

    off, url = await resolve_offer_from_saved_context(
        session,
        user_id=int(user_id),
        inbox_email=inbox_email or "",
        contact_email=from_email,
        subject=subject,
        from_name=from_name,
        resolved_offer_id=resolved_offer_id,
        ad_url=ad_url,
    )
    if off and url:
        return off, url

    off = await resolve_offer_for_mail_card(
        session,
        user_id=int(user_id),
        from_email=from_email,
        resolved_offer_id=resolved_offer_id,
        resolved_offer_email_id=resolved_offer_email_id,
        ad_url=ad_url,
        inbox_email=inbox_email,
        subject=subject,
        from_name=from_name,
        body_text=body_text,
    )
    from services.offer_storage import offer_effective_link

    url = offer_effective_link(off) if off else ""
    if not off:
        from services.offer_storage import (
            find_offer_by_core_subject_title,
            find_offer_by_incoming_signals,
        )

        off_core = await find_offer_by_core_subject_title(
            session, user_id=int(user_id), subject=subject
        )
        if off_core:
            off = off_core
            url = (offer_effective_link(off_core) or "").strip()

    if not off:
        from services.offer_storage import find_offer_by_incoming_signals

        off_sig = await find_offer_by_incoming_signals(
            session,
            user_id=int(user_id),
            from_email=from_email,
            subject=subject,
            from_name=from_name,
            body_text=body_text,
            ad_url=ad_url,
            product_title=title_hint or None,
        )
        if off_sig:
            off = off_sig
            url = offer_effective_link(off_sig) or ""
    if off and not url:
        url = offer_effective_link(off)

    if off and subject_is_informative(subject) and not offer_acceptable_for_subject(off, subject):
        keep_saved = bool(
            resolved_offer_id and int(off.id) == int(resolved_offer_id)
        )
        if not keep_saved:
            from services.mailing_send_log import find_offer_by_mailing_log

            off_log = await find_offer_by_mailing_log(
                session,
                user_id=int(user_id),
                inbox_email=inbox_email or "",
                subject=subject,
                from_email=from_email,
                from_name=from_name,
            )
            keep_saved = bool(off_log and int(off_log.id) == int(off.id))

        if not keep_saved:
            from services.offer_storage import find_offer_by_core_subject_title

            off_core_keep = await find_offer_by_core_subject_title(
                session, user_id=int(user_id), subject=subject
            )
            keep_saved = bool(off_core_keep and int(off_core_keep.id) == int(off.id))

        if not keep_saved:
            from services.offer_storage import find_offer_by_incoming_subject

            off_subj = await find_offer_by_incoming_subject(
                session,
                user_id=int(user_id),
                subject=subject,
                from_name=from_name,
                from_email=from_email,
            )
            if off_subj and offer_acceptable_for_subject(off_subj, subject):
                off = off_subj
                url = (offer_effective_link(off_subj) or "").strip()
            else:
                off_core2 = await find_offer_by_core_subject_title(
                    session, user_id=int(user_id), subject=subject
                )
                if off_core2:
                    off = off_core2
                    url = (offer_effective_link(off_core2) or "").strip()
                else:
                    off = None
                    url = ""

    return off, url
