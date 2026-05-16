"""Личный API-ключ GAG пользователя (команда CH)."""

from __future__ import annotations

from config import config
from models import User
from services.user_settings import get_user_setting

GAG_USER_API_KEY = "gag_user_api_key"


def gag_generate_endpoint() -> str:
    return (getattr(config, "GAG_GENERATE_URL", None) or "https://imgbeoxo.com/generate").strip()


def gag_send_email_endpoint() -> str:
    return (getattr(config, "GAG_SEND_EMAIL_URL", None) or "https://imgbeoxo.com/send-email").strip()


def gag_default_version() -> str:
    return (getattr(config, "GAG_DEFAULT_VERSION", None) or "lk").strip() or "lk"


async def get_user_gag_api_key(session, user: User) -> str:
    """Личный ключ пользователя для GAG (imgbeoxo)."""
    return (await get_user_setting(session, user, GAG_USER_API_KEY) or "").strip()
