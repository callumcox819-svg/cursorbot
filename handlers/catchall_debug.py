import logging
import os

from aiogram import Router
from aiogram.types import Message, CallbackQuery

router = Router()

# Если нужно временно вернуть отладочные ответы в Telegram:
# Railway env: DEBUG_CATCHALL=1
DEBUG_CATCHALL = os.getenv("DEBUG_CATCHALL", "").strip() == "1"


@router.message()
async def _catch_any_message(message: Message):
    txt = message.text or ""
    logging.info("[CATCHALL] message from=%s text=%r", getattr(message.from_user, "id", None), txt)

    # ✅ По умолчанию НИЧЕГО не отправляем в чат
    if not DEBUG_CATCHALL:
        return

    await message.answer(
        "🧯 DEBUG: сообщение дошло до бота\n"
        f"TEXT: {txt}\n"
        f"REPR: {txt!r}\n"
        f"FROM: {getattr(message.from_user, 'id', None)}"
    )


@router.callback_query()
async def _catch_any_callback(call: CallbackQuery):
    data = call.data or ""
    logging.warning("[CATCHALL] callback from=%s data=%r", getattr(call.from_user, "id", None), data)

    # ✅ Чтобы Telegram не крутил "loading"
    if not DEBUG_CATCHALL:
        try:
            await call.answer()
        except Exception:
            pass
        return

    await call.answer(f"🧯 DEBUG: callback дошёл\nDATA: {data!r}", show_alert=True)
