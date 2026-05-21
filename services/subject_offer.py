"""Глобальная тема письма с подстановкой OFFER (название товара)."""

from __future__ import annotations

import re

from config import config


def sanitize_email_subject(text: str) -> str:
    """Тема письма — одна строка без \\n (иначе SMTP: HeaderWriteError)."""
    s = (text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def global_subject_template() -> str:
    tpl = (getattr(config, "GLOBAL_SUBJECT_TEMPLATE", None) or "OFFER").strip()
    return tpl or "OFFER"


def render_subject_with_offer(subject_template: str, offer_title: str) -> str:
    tpl = sanitize_email_subject((subject_template or "").strip() or global_subject_template())
    offer_value = sanitize_email_subject((offer_title or "").strip() or "OFFER")
    out = tpl.replace("{{OFFER}}", offer_value).replace("OFFER", offer_value).strip()
    if not out:
        out = offer_value
    out = sanitize_email_subject(out)
    if len(out) > 140:
        out = out[:137] + "…"
    return out


def subject_for_offer(offer_title: str) -> str:
    from services.text_ascii import fold_plain_mail_text

    subj = render_subject_with_offer(global_subject_template(), offer_title)
    return fold_plain_mail_text(subj)
