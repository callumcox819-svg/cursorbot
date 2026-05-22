"""Глобальная тема письма: OFFER + ротация шаблонов на каждое письмо."""

from __future__ import annotations

import os
import re

from config import config

SUBJECT_ROTATION_INDEX_KEY = "subject_rotation_index"

# Глобальные темы (по кругу: 1-е письмо → #1, 2-е → #2, 3-е → #3, 4-е → #1 …)
DEFAULT_ROTATION_SUBJECT_TEMPLATES: tuple[str, ...] = (
    "Anfrage zu OFFER",
    "Kurze Frage: OFFER",
    "OFFER — noch verfuegbar?",
)


def sanitize_email_subject(text: str) -> str:
    """Тема письма — одна строка без \\n (иначе SMTP: HeaderWriteError)."""
    s = (text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def rotation_subject_templates() -> tuple[str, ...]:
    """Список тем для ротации. Переопределение: GLOBAL_SUBJECT_TEMPLATES через |."""
    raw = (os.getenv("GLOBAL_SUBJECT_TEMPLATES") or "").strip()
    if raw:
        parts = tuple(p.strip() for p in raw.split("|") if p.strip())
        if parts:
            return parts
    return DEFAULT_ROTATION_SUBJECT_TEMPLATES


def global_subject_template() -> str:
    """Первый шаблон ротации (совместимость со старым GLOBAL_SUBJECT_TEMPLATE)."""
    legacy = (getattr(config, "GLOBAL_SUBJECT_TEMPLATE", None) or "").strip()
    if legacy and legacy != "OFFER":
        return legacy
    return rotation_subject_templates()[0]


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


def subject_for_offer(offer_title: str, *, rotation_index: int = 0) -> str:
    """Тема с подстановкой OFFER; rotation_index — какой шаблон из глобальной ротации."""
    from services.text_ascii import fold_plain_mail_text

    templates = rotation_subject_templates()
    tpl = templates[int(rotation_index) % len(templates)]
    subj = render_subject_with_offer(tpl, offer_title)
    return fold_plain_mail_text(subj)


async def load_subject_rotation_index(session, user_id: int) -> int:
    from services.user_settings import get_user_setting

    raw = await get_user_setting(session, int(user_id), SUBJECT_ROTATION_INDEX_KEY)
    try:
        return max(0, int((raw or "0").strip()))
    except ValueError:
        return 0


async def save_subject_rotation_index(session, user_id: int, value: int) -> None:
    from services.user_settings import set_user_setting

    await set_user_setting(
        session,
        int(user_id),
        SUBJECT_ROTATION_INDEX_KEY,
        str(int(value) % 1_000_000_000),
    )


_OUTGOING_SUBJ_PREFIX_RE = re.compile(
    r"^(?:anfrage zu|kurze frage)\s*:?\s*",
    re.IGNORECASE,
)
_OUTGOING_SUBJ_SUFFIX_RE = re.compile(
    r"\s*[—\-]\s*noch verfuegbar\?\s*$",
    re.IGNORECASE,
)


def extract_core_offer_title_from_subject(subject: str) -> str:
    """
    Из темы ответа Re: / из исходящей темы рассылки — только название лота
    (без «Kurze Frage:», «Anfrage zu», «— noch verfuegbar?»).
    """
    from services.offer_matching import normalized_reply_subject

    s = normalized_reply_subject(subject or "")
    if not s:
        return ""
    s = _OUTGOING_SUBJ_PREFIX_RE.sub("", s).strip()
    s = _OUTGOING_SUBJ_SUFFIX_RE.sub("", s).strip()
    return sanitize_email_subject(s)


def subjects_same_for_mailing(sent_subject: str, reply_subject: str) -> bool:
    """Тема ответа = тема исходящего письма /send (с учётом Re: и шаблонов)."""
    from services.offer_matching import normalized_reply_subject
    from services.offer_storage import _title_compact

    a = normalized_reply_subject(sent_subject or "")
    b = normalized_reply_subject(reply_subject or "")
    if a and b and a == b:
        return True
    ca = _title_compact(extract_core_offer_title_from_subject(sent_subject))
    cb = _title_compact(extract_core_offer_title_from_subject(reply_subject))
    if ca and cb and (ca == cb or ca in cb or cb in ca):
        return True
    return False


def rotation_templates_preview() -> str:
    """Текст для настроек / подсказок."""
    return "\n".join(
        f"{i + 1}. <code>{sanitize_email_subject(t)}</code>"
        for i, t in enumerate(rotation_subject_templates())
    )
