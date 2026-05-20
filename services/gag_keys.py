"""Личный API-ключ GAG пользователя (команда CH)."""

from __future__ import annotations

from config import config
from models import User
from services.user_settings import get_user_setting

GAG_USER_API_KEY = "gag_user_api_key"

# Коды сервисов для imgbeoxo /generate (поле service)
GAG_SERVICE_CHOICES = ("tutti_ch", "posta_ch", "ricardo_ch")

_SERVICE_ALIASES: dict[str, str] = {
    "tutti_ch": "tutti_ch",
    "tutti.ch": "tutti_ch",
    "post_ch": "posta_ch",
    "posta_ch": "posta_ch",
    "post.ch": "posta_ch",
    "ricardo_ch": "ricardo_ch",
    "ricardo.ch": "ricardo_ch",
}


def normalize_gag_service(code: str | None) -> str | None:
    """Канонический код сервиса для API и настроек."""
    s = (code or "").strip().lower()
    if not s:
        return None
    return _SERVICE_ALIASES.get(s)


def is_valid_gag_service(code: str | None) -> bool:
    return normalize_gag_service(code) is not None


def gag_service_for_api(code: str | None) -> str:
    """Значение поля service в POST /generate."""
    n = normalize_gag_service(code)
    if not n:
        raise ValueError(f"Unknown GAG service: {code!r}")
    return n


def gag_service_for_html_dir(code: str | None) -> str:
    """Имя папки в data/HTMLch/ (у ПОСТ в репозитории — post_ch)."""
    n = normalize_gag_service(code) or ""
    if n == "posta_ch":
        return "post_ch"
    return n


def gag_service_matches(cur: str | None, choice: str) -> bool:
    """Сравнение выбранного сервиса с учётом post_ch / posta_ch."""
    a = normalize_gag_service(cur)
    b = normalize_gag_service(choice)
    return bool(a and b and a == b)


def gag_service_label(code: str | None) -> str:
    n = normalize_gag_service(code) or (code or "").strip()
    return {
        "tutti_ch": "ТУТТИ",
        "posta_ch": "ПОСТ (posta_ch)",
        "ricardo_ch": "Ricardo.ch",
    }.get(n, n or "—")


def gag_service_from_offer_link(link: str, *, user_fallback: str | None = None) -> str | None:
    """Сервис GAG из ссылки объявления (не из глобальных настроек пользователя)."""
    l = (link or "").lower()
    if "ricardo.ch" in l:
        return "ricardo_ch"
    if "facebook.com" in l or "fb.com/marketplace" in l:
        return "tutti_ch"
    if "tutti.ch" in l:
        return "tutti_ch"
    if "post.ch" in l or "posta.ch" in l:
        return "posta_ch"
    if "kleinanzeigen" in l or "ebay." in l:
        return "posta_ch"
    return normalize_gag_service(user_fallback)


def gag_generate_endpoint() -> str:
    return (getattr(config, "GAG_GENERATE_URL", None) or "https://imgbeoxo.com/generate").strip()


def gag_send_email_endpoint() -> str:
    return (getattr(config, "GAG_SEND_EMAIL_URL", None) or "https://imgbeoxo.com/send-email").strip()


def gag_default_version() -> str:
    return (getattr(config, "GAG_DEFAULT_VERSION", None) or "lk").strip() or "lk"


async def get_user_gag_api_key(session, user: User) -> str:
    """Личный ключ пользователя для GAG (imgbeoxo)."""
    return (await get_user_setting(session, user, GAG_USER_API_KEY) or "").strip()
