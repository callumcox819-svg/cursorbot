"""Глобальная тема письма с подстановкой OFFER (название товара)."""

from __future__ import annotations

from config import config


def global_subject_template() -> str:
    tpl = (getattr(config, "GLOBAL_SUBJECT_TEMPLATE", None) or "OFFER").strip()
    return tpl or "OFFER"


def render_subject_with_offer(subject_template: str, offer_title: str) -> str:
    tpl = (subject_template or "").strip() or global_subject_template()
    offer_value = (offer_title or "").strip() or "OFFER"
    out = tpl.replace("{{OFFER}}", offer_value).replace("OFFER", offer_value).strip()
    if not out:
        out = offer_value
    if len(out) > 140:
        out = out[:137] + "…"
    return out


def subject_for_offer(offer_title: str) -> str:
    return render_subject_with_offer(global_subject_template(), offer_title)
