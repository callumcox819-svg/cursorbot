# database.py
from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from models import Base

log = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Локальный запуск/тесты: если DATABASE_URL не задан — используем SQLite.
# Это убирает падение вида: "Could not parse SQLAlchemy URL from string ''".
if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///./bot.db"
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_NAME"):
        log.error(
            "Railway: переменная DATABASE_URL ПУСТАЯ. Удалите пустую переменную в Variables, "
            "добавьте Reference на Postgres → DATABASE_URL (или вставьте URL из Postgres → Connect). "
            "См. RAILWAY_DATABASE.txt"
        )
    else:
        log.warning("DATABASE_URL is empty. Falling back to %s", DATABASE_URL)

# Railway часто отдаёт postgres://, а SQLAlchemy asyncpg хочет postgresql+asyncpg://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": 60}
elif DATABASE_URL.startswith("postgresql"):
    _engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "15"))
    _engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "25"))
    _engine_kwargs["pool_timeout"] = int(os.getenv("DB_POOL_TIMEOUT", "30"))

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()

# ✅ основная фабрика сессий (как ждут handlers/send.py и др.)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# ✅ алиас для старого кода (как ждёт middlewares/access.py)
Session = async_session


@asynccontextmanager
async def db_session():
    """Postgres/SQLite сессия со сбросом PySocks-патча (иначе пул зависает под SMTP)."""
    from proxy_manager import database_socket_guard

    async with database_socket_guard():
        async with Session() as session:
            yield session


async def _ensure_users_telegram_bigint() -> None:
    """
    Автомиграция: users.telegram_id INTEGER -> BIGINT
    Чтобы Telegram ID типа 7416000184 не ломал запросы.
    """
    # Эти миграции написаны под Postgres (information_schema, ::bigint).
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        res = await conn.execute(
            text("""
                SELECT data_type
                FROM information_schema.columns
                WHERE table_name='users' AND column_name='telegram_id'
                LIMIT 1
            """)
        )
        row = res.first()
        if not row:
            return

        data_type = (row[0] or "").lower()
        if data_type in ("integer", "int4"):
            log.warning("Migrating users.telegram_id from INTEGER to BIGINT ...")
            await conn.execute(text("ALTER TABLE users ALTER COLUMN telegram_id TYPE BIGINT USING telegram_id::bigint"))
            log.warning("Migrated users.telegram_id to BIGINT ✅")


async def _ensure_incoming_mail_telegram_message_id_column() -> None:
    """Не дублировать карточку входящего в TG при повторном IMAP-опросе."""
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT")
        )


async def _ensure_incoming_mail_link_columns() -> None:
    """Автомиграция: добавляем incoming_mails.ad_url и incoming_mails.generated_link если их нет.

    В проекте нет alembic-миграций, поэтому добавляем колонки безопасно через information_schema.
    """
    # Postgres-only (ALTER TABLE ... IF NOT EXISTS)
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        # Railway/Postgres поддерживает IF NOT EXISTS для ADD COLUMN
        await conn.execute(text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS ad_url TEXT"))
        await conn.execute(text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS generated_link TEXT"))


async def _ensure_mailing_send_snapshot_columns() -> None:
    """Снимок товара в журнале /send — сервис, фото, цена, ссылка на момент рассылки."""
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE mailing_sends ADD COLUMN IF NOT EXISTS service_label VARCHAR")
        )
        await conn.execute(
            text("ALTER TABLE mailing_sends ADD COLUMN IF NOT EXISTS photo_url TEXT")
        )
        await conn.execute(
            text("ALTER TABLE mailing_sends ADD COLUMN IF NOT EXISTS offer_price VARCHAR")
        )
        await conn.execute(
            text("ALTER TABLE mailing_sends ADD COLUMN IF NOT EXISTS ad_url_snapshot TEXT")
        )


async def _ensure_incoming_mail_snapshot_columns() -> None:
    """Снимок товара/сервиса/фото на входящем — не теряется при повторном открытии карточки."""
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS product_title TEXT")
        )
        await conn.execute(
            text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS service_label VARCHAR")
        )
        await conn.execute(
            text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS photo_url TEXT")
        )
        await conn.execute(
            text("ALTER TABLE incoming_mails ADD COLUMN IF NOT EXISTS offer_price VARCHAR")
        )


async def _ensure_conversation_links_generated_link_column() -> None:
    """Автомиграция: добавляем conversation_links.generated_link если её нет.

    Нужна для кнопки "Создать ссылку" (генерация ссылки сохраняется в ConversationLink).
    """
    # Postgres-only (ALTER TABLE ... IF NOT EXISTS)
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE conversation_links ADD COLUMN IF NOT EXISTS generated_link TEXT"))


async def _ensure_offers_raw_json_column() -> None:
    """Полный JSON объявления из парсера (все поля для генерации ссылок)."""
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE offers ADD COLUMN IF NOT EXISTS raw_json TEXT"))


async def _migrate_seller_blacklist_names() -> None:
    """ЧС продавцов по имени из JSON (не по email)."""
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE seller_blacklist ADD COLUMN IF NOT EXISTS seller_name_key TEXT"))
        await conn.execute(text("ALTER TABLE seller_blacklist ADD COLUMN IF NOT EXISTS seller_name_display TEXT"))
        await conn.execute(text("ALTER TABLE seller_blacklist DROP COLUMN IF EXISTS seller_email"))
        await conn.execute(text("ALTER TABLE seller_blacklist DROP COLUMN IF EXISTS note"))


async def _ensure_conversation_links_pinned_offer_id_column() -> None:
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE conversation_links ADD COLUMN IF NOT EXISTS pinned_offer_id INTEGER")
        )


async def _ensure_conversation_links_tg_message_id_column() -> None:
    """Автомиграция: добавляем conversation_links.tg_message_id если её нет.

    Нужна для трединга в Telegram: повторные сообщения от продавца должны отправляться
    reply_to первому сообщению.
    """
    if engine.dialect.name != "postgresql":
        return

    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE conversation_links ADD COLUMN IF NOT EXISTS tg_message_id BIGINT"))


async def _ensure_email_accounts_proxy_id_column() -> None:
    """Привязка ящика к SOCKS5 (proxy_id), без ротации IP."""
    async with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            await conn.execute(
                text("ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS proxy_id INTEGER")
            )
        else:
            res = await conn.execute(text("PRAGMA table_info(email_accounts)"))
            cols = [row[1] for row in res.fetchall()]
            if "proxy_id" not in cols:
                await conn.execute(text("ALTER TABLE email_accounts ADD COLUMN proxy_id INTEGER"))


async def init_db() -> None:
    dialect = engine.dialect.name
    if dialect == "postgresql":
        host_hint = ""
        try:
            from urllib.parse import urlparse

            p = urlparse(DATABASE_URL.split("+", 1)[-1] if "+" in DATABASE_URL else DATABASE_URL)
            host_hint = f" host={p.hostname or '?'}"
        except Exception:
            pass
        log.info(
            "БД: PostgreSQL (данные сохраняются между перезапусками Railway)%s",
            host_hint,
        )
    else:
        log.warning(
            "БД: %s — для Railway добавьте Postgres и переменную DATABASE_URL",
            dialect,
        )
        if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
            log.error(
                "RAILWAY без PostgreSQL: аккаунты/офферы/входящие пропадут при redeploy! "
                "Postgres → Variables → DATABASE_URL (Reference). См. RAILWAY_DATABASE.txt"
            )

    # создаём таблицы если нет
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # затем — безопасная миграция типов
    try:
        await _ensure_users_telegram_bigint()
    except Exception as e:
        log.error("Failed users.telegram_id BIGINT migration: %s", e)

    # затем — безопасное добавление колонок в incoming_mails (для "Инфо" и генерации ссылок)
    try:
        await _ensure_incoming_mail_link_columns()
    except Exception as e:
        log.error("Failed incoming_mails link columns migration: %s", e)

    try:
        await _ensure_incoming_mail_snapshot_columns()
    except Exception as e:
        log.error("Failed incoming_mails snapshot columns migration: %s", e)

    try:
        await _ensure_mailing_send_snapshot_columns()
    except Exception as e:
        log.error("Failed mailing_sends snapshot columns migration: %s", e)

    try:
        await _ensure_incoming_mail_telegram_message_id_column()
    except Exception as e:
        log.error("Failed incoming_mails.telegram_message_id migration: %s", e)

    # затем — безопасное добавление колонки в conversation_links (для "Создать ссылку")
    try:
        await _ensure_conversation_links_generated_link_column()
    except Exception as e:
        log.error("Failed conversation_links.generated_link migration: %s", e)

    # затем — tg_message_id для трединга входящих сообщений
    try:
        await _ensure_conversation_links_tg_message_id_column()
    except Exception as e:
        log.error("Failed conversation_links.tg_message_id migration: %s", e)

    try:
        await _ensure_offers_raw_json_column()
    except Exception as e:
        log.error("Failed offers.raw_json migration: %s", e)

    if dialect == "postgresql":
        try:
            async with engine.begin() as conn:
                has_ms = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_name = 'mailing_sends' LIMIT 1"
                        )
                    )
                ).first()
                if not has_ms:
                    log.warning(
                        "Таблица mailing_sends отсутствует — пересоздайте схему (restart bot) "
                        "или выполните create_all"
                    )
        except Exception as e:
            log.error("mailing_sends table check failed: %s", e)

    try:
        await _ensure_conversation_links_pinned_offer_id_column()
    except Exception as e:
        log.error("Failed conversation_links.pinned_offer_id migration: %s", e)

    try:
        await _migrate_seller_blacklist_names()
    except Exception as e:
        log.error("Failed seller_blacklist name migration: %s", e)

    try:
        await _ensure_email_accounts_proxy_id_column()
    except Exception as e:
        log.error("Failed email_accounts.proxy_id migration: %s", e)
