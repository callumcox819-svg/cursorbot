from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def settings_menu() -> InlineKeyboardMarkup:
    """Клавиатура настроек (сеткой, как в референсе)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📬 Аккаунты", callback_data="settings_accounts"),
                InlineKeyboardButton(text="📂 Домены", callback_data="settings_domains"),
            ],
            [
                InlineKeyboardButton(text="🌍 СТРАНЫ", callback_data="settings_countries"),
            ],
            [
                InlineKeyboardButton(text="🧩 Команда", callback_data="goo_team_menu"),
                InlineKeyboardButton(text="👤 Профиль", callback_data="goo_show:profile"),
            ],
            [
                InlineKeyboardButton(text="🔑 Ключ", callback_data="goo_show:key"),
                InlineKeyboardButton(text="📝 Имя отправки", callback_data="sender_name_menu"),
            ],
            [
                InlineKeyboardButton(text="📝 Смена ника", callback_data="html_nick_menu"),
                InlineKeyboardButton(text="🌐 Прокси", callback_data="settings_proxies"),
            ],
            [
                InlineKeyboardButton(text="🧾 Шаблоны", callback_data="settings_templates"),
                InlineKeyboardButton(text="⏱ Тайминги", callback_data="settings_timings"),
            ],
            [InlineKeyboardButton(text="💬 Первые смс", callback_data="firstsms_open")],
            [InlineKeyboardButton(text="🤖 Авто-ответ", callback_data="settings_auto")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_back")],
        ]
    )
