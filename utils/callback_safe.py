"""Безопасный ответ на CallbackQuery: не роняет апдейт при ошибке или таймауте answer."""

from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)

_ANSWER_TIMEOUT_SEC = 8.0


async def callback_answer_safe(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        await asyncio.wait_for(
            callback.answer(text=text, show_alert=show_alert),
            timeout=_ANSWER_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning("callback.answer timeout (data=%r)", callback.data)
    except TelegramBadRequest as e:
        logger.warning("callback.answer пропущен (TelegramBadRequest): %s", e)
    except Exception:
        logger.exception("callback.answer error (data=%r)", callback.data)
