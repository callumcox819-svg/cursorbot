"""
Отдельный процесс только для входящей почты (IMAP).

На Railway: второй сервис из того же репозитория, команда запуска:
  python imap_worker.py

Общее с ботом: DATABASE_URL, BOT_TOKEN (только send_message, без polling).
На сервисе бота: IMAP_DEDICATED_WORKER=1 — чтобы IMAP не дублировался в bot.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from config import config
from database import init_db

logger = logging.getLogger(__name__)


def _truthy(name: str, default: str = "") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


async def _worker_heartbeat() -> None:
    n = 0
    while True:
        await asyncio.sleep(60)
        n += 1
        try:
            from services.incoming_mail_worker import incoming_mail_diag_snapshot

            diag = incoming_mail_diag_snapshot()
        except Exception:
            diag = {}
        logger.info(
            "💓 IMAP worker alive #%s max_concurrent=%s backoff_accounts=%s",
            n,
            diag.get("max_concurrent", "?"),
            len(diag.get("backoff_sec_by_account") or {}),
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not _truthy("ENABLE_INCOMING_MAIL"):
        logger.error(
            "ENABLE_INCOMING_MAIL не задан. На сервисе imap-worker в Variables: ENABLE_INCOMING_MAIL=1"
        )
        sys.exit(1)

    if not (config.BOT_TOKEN or "").strip():
        logger.error("BOT_TOKEN пустой — нужен для пересылки писем в Telegram")
        sys.exit(1)

    await init_db()
    from database import engine as db_engine

    logger.info("IMAP worker: БД %s, polling Telegram НЕ запускается", db_engine.dialect.name)

    http_timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT_SEC", "35"))
    session = AiohttpSession(timeout=http_timeout)
    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    me = await bot.get_me()
    logger.info("IMAP worker: Bot @%s (id=%s) — только исходящие уведомления", me.username, me.id)

    # На dedicated-воркере по умолчанию выше параллелизм, чем в bot.py (там 6).
    os.environ.setdefault("MAX_IMAP_CONCURRENT", "16")
    os.environ.setdefault("IMAP_MAILING_PAUSE", "slow")

    poll_seconds = int(os.getenv("INCOMING_MAIL_POLL_SECONDS", "20"))
    delay = int(os.getenv("INCOMING_MAIL_START_DELAY_SEC", "15"))
    if delay > 0:
        logger.info("Старт опроса ящиков через %ss", delay)
        await asyncio.sleep(delay)

    from services.incoming_mail_worker import start_incoming_mail_worker

    start_incoming_mail_worker(bot, poll_seconds=poll_seconds)
    asyncio.create_task(_worker_heartbeat())

    try:
        await asyncio.Event().wait()
    finally:
        await bot.session.close()
        logger.info("IMAP worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
