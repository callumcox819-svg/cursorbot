from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def settings_menu() -> InlineKeyboardMarkup:
    """Назад в главное меню настроек (Швейцария / GAG)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )
