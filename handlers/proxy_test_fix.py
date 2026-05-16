import asyncio
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from services.proxy_autochecker import check_proxy

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("proxy_test:"))
async def proxy_test(callback: CallbackQuery):
    # Главное: ответить мгновенно, иначе Telegram скажет "query is too old"
    try:
        await callback.answer("⏳ Тестирую прокси...", show_alert=False)
    except TelegramBadRequest:
        pass

    proxy = callback.data.split("proxy_test:", 1)[1].strip()
    chat_id = callback.message.chat.id

    async def run():
        try:
            r = await check_proxy(proxy, timeout=10)
            if r.ok:
                text = (
                    f"✅ Прокси OK\n"
                    f"Тип: {r.kind}\n"
                    f"Прокси: {r.proxy}\n"
                    f"IP: {r.ip or 'unknown'}"
                )
            else:
                text = (
                    f"❌ Прокси НЕ РАБОТАЕТ\n"
                    f"Тип: {r.kind}\n"
                    f"Прокси: {r.proxy}\n"
                    f"Ошибка: {r.error}"
                )
            await callback.bot.send_message(chat_id, text)
        except Exception as e:
            logger.exception("proxy_test failed: %r", e)
            await callback.bot.send_message(chat_id, f"❌ Ошибка теста прокси: {e}")

    asyncio.create_task(run())
