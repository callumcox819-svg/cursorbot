"""Тема и имя отправителя для HTML-ответов (отдельно от рассылки с OFFER)."""

from __future__ import annotations

from models import User
from services.html_spoof import apply_nick_to_html, get_spoof_display_name
from services.user_settings import get_user_setting

# как в handlers/settings.py → html_theme_menu
HTML_THEME_KEY = "html_theme"


async def get_html_reply_subject(session, user: User, *, fallback: str = "") -> str:
    """
    Тема для HTML: задаёт пользователь в настройках (📌 Тема HTML).
    Не путать с глобальным OFFER для массовой рассылки.
    """
    subj = (await get_user_setting(session, user, HTML_THEME_KEY) or "").strip()
    if subj:
        return subj[:140] if len(subj) > 140 else subj
    fb = (fallback or "").strip()
    return fb[:140] if len(fb) > 140 else (fb or "Message")


async def get_html_sender_name(session, user: User) -> str | None:
    """
    HTML: при 🟢 Спуфинг — имя из «👤 Имя для спуфинга».
    Иначе — имя отправителя из аккаунтов (user.sender_name), как при рассылке.
    """
    spoof = await get_spoof_display_name(session, user)
    if spoof:
        return spoof
    name = (getattr(user, "sender_name", None) or "").strip()
    return name or None


async def prepare_html_body(html: str, session, user: User) -> str:
    nick = await get_spoof_display_name(session, user)
    return apply_nick_to_html(html, nick)
