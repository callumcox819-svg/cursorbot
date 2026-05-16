import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from keyboards.main_menu import main_menu_kb

logger = logging.getLogger(__name__)

router = Router(name="stopsend")


@router.message(Command("stopsend"))
@router.message(F.text == "⏹ Остановить рассылку")
async def cmd_stopsend(message: Message) -> None:
    """
    Пользовательская команда остановки рассылки.
    """
    from handlers.send import get_sending_state

    user_id = message.from_user.id
    state = get_sending_state(user_id)

    if not state:
        await message.answer(
            "Сейчас для тебя нет активной рассылки.\n"
            "Запустить можно командой /send или кнопкой в меню.",
            reply_markup=main_menu_kb(message.from_user.id),
        )
        return

    if state.is_stopping:
        await message.answer(
            "Рассылка уже помечена на остановку.\n"
            "Через пару минут она завершится.",
            reply_markup=main_menu_kb(message.from_user.id),
        )
        return

    state.is_stopping = True
    await message.answer(
        "⏹ Я пометил рассылку на остановку.\n"
        "После отправки ближайших писем процесс завершится.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


# ============================================================
# 🔒 КРИТИЧНО: API ДЛЯ send.py (БЕЗ НОВОЙ ЛОГИКИ)
# ============================================================
def stop_sending_for_user(user_id: int) -> bool:
    """
    Вызывается из handlers/send.py.
    Делает ровно то же самое, что и команда /stopsend,
    но без Telegram Message.
    """
    from handlers.send import get_sending_state

    state = get_sending_state(user_id)
    if not state:
        return False

    state.is_stopping = True
    return True
