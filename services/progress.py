import time
import asyncio
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter


@dataclass
class ProgressReporter:
    bot: Bot
    chat_id: int
    message_id: int
    min_interval: float = 20.0
    _last_sent: float = field(default=0.0)

    async def update(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_sent) < self.min_interval:
            return

        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                disable_web_page_preview=True,
            )
            self._last_sent = now
        except TelegramBadRequest as e:
            # Ignore no-op edits.
            if "message is not modified" in str(e).lower():
                return
            raise
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                disable_web_page_preview=True,
            )
            self._last_sent = time.monotonic()
