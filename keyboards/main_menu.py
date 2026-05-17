from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from services.bot_roles import user_is_admin

# Тексты reply-кнопок главного меню (для middleware и приоритетных хендлеров).
MAIN_MENU_TEXTS: frozenset[str] = frozenset(
    {
        "⚙️ Настройки",
        "Настройки",
        "⚡ Быстрое добавление",
        "⚡ Быстрое добавление (Gmail)",
        "▶️ Запустить рассылку",
        "⏹ Остановить рассылку",
        "/stop",
        "/stopsend",
        "📊 Статус рассылки",
        "📬 Мои аккаунты",
        "📬 Почтовые аккаунты",
        "🧪 Тест маил",
        "👑 Админ-панель",
        "🔥 Админ-панель",
    }
)


def is_main_menu_text(text: str | None) -> bool:
    t = (text or "").strip()
    if t in MAIN_MENU_TEXTS:
        return True
    tl = t.casefold().replace("\ufe0f", "")
    return "настройки" in tl


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
