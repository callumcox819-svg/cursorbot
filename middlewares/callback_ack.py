"""Сразу снимает «часики» у inline-кнопки, пока идёт запрос к БД/SMTP."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from utils.callback_safe import callback_answer_safe

logger = logging.getLogger(__name__)


class CallbackAckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            await callback_answer_safe(event)
            logger.debug("callback ack tg=%s data=%r", event.from_user.id, event.data)
        return await handler(event, data)
