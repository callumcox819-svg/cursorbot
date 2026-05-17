from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, ReplyKeyboardRemove

from database import db_session
from keyboards.main_menu import is_main_menu_text
from services.bot_roles import config_admin_ids
from services.users import get_or_create_user

logger = logging.getLogger(__name__)

# Кэш доступа — без запроса в БД на каждую кнопку (главная причина «подлагиваний»).
_ACCESS_CACHE: dict[int, tuple[bool, bool, float]] = {}
_ACCESS_CACHE_TTL_SEC = float(__import__("os").getenv("BOT_ACCESS_CACHE_TTL_SEC", "25"))
_ACCESS_DB_TIMEOUT_SEC = float(__import__("os").getenv("BOT_ACCESS_DB_TIMEOUT_SEC", "8"))

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


async def _resolve_access(telegram_id: int) -> tuple[bool, bool]:
    """(is_admin, has_bot_access) — один запрос в БД, с кэшем."""
    tg_id = int(telegram_id)
    if tg_id in config_admin_ids():
        return True, True

    now = time.monotonic()
    cached = _ACCESS_CACHE.get(tg_id)
    if cached and (now - cached[2]) < _ACCESS_CACHE_TTL_SEC:
        return cached[0], cached[1]

    is_admin = False
    has_access = False
    async with db_session() as session:
        user = await get_or_create_user(session, tg_id)
        if getattr(user, "is_banned", False):
            is_admin, has_access = False, False
        else:
            is_admin = bool(getattr(user, "is_admin", False))
            if is_admin and not bool(getattr(user, "access_granted", False)):
                user.access_granted = True
                await session.commit()
            has_access = is_admin or bool(getattr(user, "access_granted", False))

    _ACCESS_CACHE[tg_id] = (is_admin, has_access, now)
    return is_admin, has_access


async def user_has_bot_access(telegram_id: int) -> bool:
    return (await _resolve_access(int(telegram_id)))[1]


async def user_is_admin(telegram_id: int) -> bool:
    return (await _resolve_access(int(telegram_id)))[0]


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

        try:
            is_admin, has_access = await asyncio.wait_for(
                _resolve_access(int(user.id)),
                timeout=_ACCESS_DB_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error("BotAccessMiddleware: DB timeout tg=%s", user.id)
            if isinstance(event, Message) and (
                _is_start_message(event) or is_main_menu_text(event.text)
            ):
                return await handler(event, data)
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⏳ База данных занята, попробуйте через 5 сек.", show_alert=True)
                except Exception:
                    pass
            return None

        if is_admin:
            return await handler(event, data)

        if _is_start_message(event):
            return await handler(event, data)

        if has_access:
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
