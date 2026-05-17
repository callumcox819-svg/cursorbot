"""Лог входящих апдейтов Telegram (для Railway)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

logger = logging.getLogger(__name__)


class UpdateLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # На dp.update.outer_middleware приходит Update, не Message.
        if isinstance(event, Update):
            if event.message and event.message.text:
                logger.info(
                    "📩 UPDATE message tg=%s text=%r",
                    event.message.from_user.id,
                    (event.message.text or "")[:80],
                )
            elif event.callback_query:
                logger.info(
                    "📲 UPDATE callback tg=%s data=%r",
                    event.callback_query.from_user.id,
                    event.callback_query.data,
                )
        elif isinstance(event, Message) and event.text:
            logger.info("📩 message tg=%s text=%r", event.from_user.id, event.text[:80])
        elif isinstance(event, CallbackQuery):
            logger.info("📲 callback tg=%s data=%r", event.from_user.id, event.data)

        return await handler(event, data)
