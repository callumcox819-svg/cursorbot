from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, ReplyKeyboardRemove

from database import Session
from services.bot_roles import config_admin_ids, user_is_admin
from services.users import get_or_create_user

logger = logging.getLogger(__name__)

ACCESS_DENIED_TEXT = (
    "⛔ У тебя нет доступа к использованию этого бота. Обратись к администратору."
)


def _is_start_message(event: TelegramObject) -> bool:
    if not isinstance(event, Message):
        return False
    text = (event.text or "").strip()
    return text.startswith("/start")


def _is_non_private_message(event: TelegramObject) -> bool:
    """В группах/каналах не проверяем доступ по Message (пин, сервисные апдейты и т.д.)."""
    if not isinstance(event, Message):
        return False
    chat = event.chat
    if chat is None:
        return False
    return chat.type in ("group", "supergroup", "channel")


def _is_service_message(event: Message) -> bool:
    """Сервисные сообщения (пин, вступление в чат) не должны вызывать отказ в доступе."""
    if event.pinned_message is not None:
        return True
    if event.new_chat_members:
        return True
    if event.left_chat_member is not None:
        return True
    if event.group_chat_created or event.supergroup_chat_created:
        return True
    if event.migrate_to_chat_id or event.migrate_from_chat_id:
        return True
    return False


async def user_has_bot_access(telegram_id: int) -> bool:
    tg_id = int(telegram_id)
    if await user_is_admin(tg_id):
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

        if isinstance(event, Message):
            if _is_non_private_message(event) or _is_service_message(event):
                return await handler(event, data)
            if getattr(user, "is_bot", False):
                return await handler(event, data)

        if await user_is_admin(user.id):
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
