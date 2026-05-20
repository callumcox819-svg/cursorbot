"""Хранение JSON пользователя: Postgres на Railway, файлы — локально."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, List

from sqlalchemy import select

from database import Session, engine
from models import UserJsonBlob

log = logging.getLogger(__name__)

DATA_DIR = Path("data")

_BLOB_FILES = {
    "templates": "templates_{tg_id}.json",
    "smart_templates": "smart_templates_{tg_id}.json",
    "first_sms": "first_sms_{tg_id}.json",
    "test_mail_recipients": "test_mail_{tg_id}.json",
}


def _fs_path(telegram_id: int, blob_key: str) -> Path:
    pattern = _BLOB_FILES.get(blob_key)
    if not pattern:
        raise ValueError(f"unknown blob_key: {blob_key}")
    return DATA_DIR / pattern.format(tg_id=int(telegram_id))


def _use_postgres() -> bool:
    return engine.dialect.name == "postgresql"


async def load_json_blob(telegram_id: int, blob_key: str, *, default: Any = None) -> Any:
    if default is None:
        default = []

    tg_id = int(telegram_id)

    if _use_postgres():
        async with Session() as session:
            row = (
                await session.execute(
                    select(UserJsonBlob).where(
                        UserJsonBlob.telegram_id == tg_id,
                        UserJsonBlob.blob_key == blob_key,
                    )
                )
            ).scalar_one_or_none()
            if row and row.payload:
                try:
                    return json.loads(row.payload)
                except json.JSONDecodeError:
                    log.warning("Bad JSON in user_json_blobs tg=%s key=%s", tg_id, blob_key)

        migrated = await _migrate_from_filesystem(tg_id, blob_key)
        if migrated is not None:
            return migrated
        return default

    return _load_from_filesystem(tg_id, blob_key, default)


async def save_json_blob(telegram_id: int, blob_key: str, data: Any) -> None:
    tg_id = int(telegram_id)
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    if _use_postgres():
        async with Session() as session:
            row = (
                await session.execute(
                    select(UserJsonBlob).where(
                        UserJsonBlob.telegram_id == tg_id,
                        UserJsonBlob.blob_key == blob_key,
                    )
                )
            ).scalar_one_or_none()
            if row:
                row.payload = payload
            else:
                session.add(
                    UserJsonBlob(
                        telegram_id=tg_id,
                        blob_key=blob_key,
                        payload=payload,
                    )
                )
            await session.commit()
        return

    _save_to_filesystem(tg_id, blob_key, payload)


async def _migrate_from_filesystem(telegram_id: int, blob_key: str) -> Any | None:
    path = _fs_path(telegram_id, blob_key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("FS migrate failed %s: %s", path, e)
        return None
    await save_json_blob(telegram_id, blob_key, data)
    log.info("Migrated %s -> Postgres (tg=%s)", path.name, telegram_id)
    return data


def _load_from_filesystem(telegram_id: int, blob_key: str, default: Any) -> Any:
    path = _fs_path(telegram_id, blob_key)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_to_filesystem(telegram_id: int, blob_key: str, payload: str) -> None:
    path = _fs_path(telegram_id, blob_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


