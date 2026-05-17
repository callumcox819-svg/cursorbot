"""Лог каждого message/callback (на уровне Message, не Update)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class MessageLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            uid = getattr(event.from_user, "id", None)
            logger.info(
                "📩 MSG tg=%s text=%r",
                uid,
                (event.text or event.caption or "")[:100],
            )
        return await handler(event, data)


class CallbackLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            logger.info(
                "📲 CB tg=%s data=%r",
                event.from_user.id,
                event.data,
            )
        return await handler(event, data)
