from typing import Callable, Awaitable, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from config import config


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # если ADMINS не определён — пропускаем всех
        admins = getattr(config, "ADMINS", None)

        if admins is not None:
            try:
                admins = [int(x) for x in admins]
            except Exception:
                admins = []

            user = data.get("event_from_user")
            if user and user.id not in admins:
                return  # доступ запрещён

        return await handler(event, data)
