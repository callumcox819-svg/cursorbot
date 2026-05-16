from __future__ import annotations

import os

from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove

from config import config
from database import Session
from services.users import get_or_create_user

ACCESS_DENIED_TEXT = (
    "⛔ У тебя нет доступа к использованию этого бота. Обратись к администратору."
)


def _open_access_enabled() -> bool:
    return os.getenv("BOT_OPEN_ACCESS", "").strip().lower() in {"1", "true", "yes", "on"}


def _admin_ids() -> set[int]:
    return {int(x) for x in (getattr(config, "ADMIN_IDS", []) or [])}


async def user_has_bot_access(telegram_id: int) -> bool:
    if _open_access_enabled():
        return True
    tg_id = int(telegram_id)
    if tg_id in _admin_ids():
        return True
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        if getattr(user, "is_banned", False):
            return False
        if getattr(user, "access_granted", False):
            return True
        # Уже есть почты в боте — доступ сохраняем (например после переноса БД)
        from sqlalchemy import func, select

        from models import EmailAccount

        n_accounts = await session.scalar(
            select(func.count())
            .select_from(EmailAccount)
            .where(EmailAccount.user_id == user.id)
        )
        return bool(n_accounts and n_accounts > 0)


async def deny_access_message(message: Message) -> None:
    await message.answer(ACCESS_DENIED_TEXT, reply_markup=ReplyKeyboardRemove())


async def ensure_message_access(message: Message) -> bool:
    if await user_has_bot_access(message.from_user.id):
        return True
    await deny_access_message(message)
    return False


async def ensure_callback_access(callback: CallbackQuery) -> bool:
    if await user_has_bot_access(callback.from_user.id):
        return True
    await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
    return False
