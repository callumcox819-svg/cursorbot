from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import config


def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="⚡ Быстрое добавление")],
        [
            KeyboardButton(text="▶️ Запустить рассылку"),
            KeyboardButton(text="⏹ Остановить рассылку"),
        ],
        [KeyboardButton(text="📊 Статус рассылки")],
    ]

    # 👑 админ-кнопка
    admin_ids = set(getattr(config, "ADMIN_IDS", []))
    if user_id in admin_ids:
        rows.append([KeyboardButton(text="👑 Админ-панель")])
        rows.append([KeyboardButton(text="🧪 Тест маил")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True
    )
