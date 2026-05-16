"""Проверка доступа (используется в /start и middleware)."""

from __future__ import annotations

from aiogram.types import Message, ReplyKeyboardRemove

from middlewares.bot_access import (
    ACCESS_DENIED_TEXT,
    deny_access_message,
    user_has_bot_access,
)

__all__ = [
    "ACCESS_DENIED_TEXT",
    "deny_access_message",
    "user_has_bot_access",
]
