"""Активный прогон подбора/валидации — остановка через /stoppodbor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationRun:
    tg_id: int
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    items_count: int = 0


_active: dict[int, ValidationRun] = {}


def register_validation_run(tg_id: int, *, items_count: int) -> ValidationRun:
    run = ValidationRun(tg_id=int(tg_id), items_count=int(items_count))
    _active[int(tg_id)] = run
    return run


def get_validation_run(tg_id: int) -> ValidationRun | None:
    return _active.get(int(tg_id))


def clear_validation_run(tg_id: int) -> None:
    _active.pop(int(tg_id), None)


def request_stop_validation(tg_id: int) -> bool:
    """True если была активная валидация и запрошена остановка."""
    run = _active.get(int(tg_id))
    if not run:
        return False
    run.cancel_event.set()
    return True


def is_validation_running(tg_id: int) -> bool:
    return int(tg_id) in _active
