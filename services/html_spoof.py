"""Спуфинг имени в HTML-письмах (только если включён ref_toggle:spoofing)."""

from __future__ import annotations

from models import User
from services.user_settings import get_user_setting

SPOOFING_KEY = "spoofing"
GAG_SERVICE_KEY = "gag_service"


def html_nick_key_for_service(service: str) -> str:
    s = (service or "").strip()
    return f"html_nick_{s}" if s else "html_nick"


def _setting_on(val: object) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "on", "y"}


async def get_spoof_display_name(session, user: User) -> str | None:
    """
    Имя для HTML / поля From, если 🟢 Спуфинг включён и задано в «👤 Имя для спуфинга».
    Иначе None — обычная отправка без подмены имени.
    """
    if not _setting_on(await get_user_setting(session, user, SPOOFING_KEY)):
        return None
    service = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
    nick = (await get_user_setting(session, user, html_nick_key_for_service(service)) or "").strip()
    return nick or None


def apply_nick_to_html(html: str, nick: str | None) -> str:
    if not nick:
        return html
    return html.replace("{{NICK}}", nick)
