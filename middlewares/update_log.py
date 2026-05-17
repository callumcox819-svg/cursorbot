"""Короткий лог входящих message/callback (для Railway)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class UpdateLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text:
            logger.info("📩 message tg=%s text=%r", event.from_user.id, event.text[:80])
        elif isinstance(event, CallbackQuery):
            logger.info("📲 callback tg=%s data=%r", event.from_user.id, event.data)
        return await handler(event, data)
