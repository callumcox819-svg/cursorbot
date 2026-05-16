from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, ReplyKeyboardRemove

from config import config
from database import Session
from services.users import get_or_create_user

logger = logging.getLogger(__name__)

ACCESS_DENIED_TEXT = (
    "⛔ У тебя нет доступа к использованию этого бота. Обратись к администратору."
)


def _admin_ids() -> set[int]:
    return {int(x) for x in (getattr(config, "ADMIN_IDS", []) or [])}


def _is_start_message(event: TelegramObject) -> bool:
    if not isinstance(event, Message):
        return False
    text = (event.text or "").strip()
    return text.startswith("/start")


async def user_has_bot_access(telegram_id: int) -> bool:
    tg_id = int(telegram_id)
    if tg_id in _admin_ids():
        return True
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
    if getattr(user, "is_banned", False):
        return False
    return bool(getattr(user, "access_granted", False))


async def deny_access_message(message: Message) -> None:
    await message.answer(ACCESS_DENIED_TEXT, reply_markup=ReplyKeyboardRemove())


class BotAccessMiddleware(BaseMiddleware):
    """Блокирует все апдейты без access_granted (кроме /start и админов)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if int(user.id) in _admin_ids():
            return await handler(event, data)

        if _is_start_message(event):
            return await handler(event, data)

        if await user_has_bot_access(user.id):
            return await handler(event, data)

        if isinstance(event, Message):
            await deny_access_message(event)
            return None

        if isinstance(event, CallbackQuery):
            try:
                await event.answer(ACCESS_DENIED_TEXT, show_alert=True)
            except Exception:
                pass
            return None

        return None
