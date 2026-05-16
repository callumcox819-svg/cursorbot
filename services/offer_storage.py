"""Сохранение объявлений из JSON парсера в БД (все поля + email после валидации)."""

from __future__ import annotations

import json
import re
from typing import Any

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


def fields_from_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "person_name": str(
            item.get("item_person_name")
            or item.get("person_name")
            or item.get("name")
            or ""
        ).strip(),
        "title": str(item.get("item_title") or item.get("title") or "").strip(),
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
