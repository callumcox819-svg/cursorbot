"""Фоновые задачи на пользователя — чтобы кнопки не блокировали polling."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

logger = logging.getLogger(__name__)

_tasks: dict[tuple[int, str], asyncio.Task] = {}


def is_running(user_id: int, key: str) -> bool:
    t = _tasks.get((int(user_id), str(key)))
    return t is not None and not t.done()


async def _wrap(user_id: int, key: str, coro: Awaitable[Any]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("background job failed user_id=%s key=%s", user_id, key)
    finally:
        _tasks.pop((int(user_id), str(key)), None)


def start(user_id: int, key: str, coro: Awaitable[Any]) -> bool:
    """Запустить фоновую задачу. False — такая же уже выполняется."""
    uid = int(user_id)
    k = str(key)
    if is_running(uid, k):
        return False
    task = asyncio.create_task(_wrap(uid, k, coro))
    _tasks[(uid, k)] = task
    return True
