from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from services.bot_roles import user_is_admin


def main_menu_kb(user_id: int, *, show_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="⚡ Быстрое добавление")],
        [
            KeyboardButton(text="▶️ Запустить рассылку"),
            KeyboardButton(text="⏹ Остановить рассылку"),
        ],
        [KeyboardButton(text="📊 Статус рассылки")],
    ]

    if show_admin:
        rows.append([KeyboardButton(text="👑 Админ-панель")])
        rows.append([KeyboardButton(text="🧪 Тест маил")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def main_menu_kb_for(user_id: int) -> ReplyKeyboardMarkup:
    """Клавиатура с учётом is_admin в БД (не только config.ADMIN_IDS)."""
    return main_menu_kb(user_id, show_admin=await user_is_admin(user_id))
