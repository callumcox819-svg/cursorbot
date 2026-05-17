"""Контроль SMTP-блокировок: ящик остаётся для IMAP, рассылка с него снимается."""

from __future__ import annotations

import html

from aiogram import Bot
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, User
from services.sender import normalize_send_error
from services.user_settings import get_user_setting


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def is_smtp_account_block_error(err: str | None) -> bool:
    """Ошибка уровня ящика (лимит Gmail, блок, неверный пароль) — не ошибка одного получателя."""
    s = normalize_send_error(err or "")
    kind = s.split("|", 1)[0].split(":", 1)[0].strip().upper()
    if kind in (
        "ACCOUNT_BLOCKED",
        "ACCOUNT_RATE_LIMIT",
        "ACCOUNT_INVALID_CREDENTIALS",
        "ACCOUNT_WEB_LOGIN_REQUIRED",
    ):
        return True
    t = s.lower()
    phrases = (
        "daily user sending limit",
        "sending limit exceeded",
        "user sending limit",
        "limit exceeded",
        "too many messages",
        "mailbox full",
        "account has been disabled",
        "web login required",
        "username and password not accepted",
        "5.4.5",
    )
    return any(p in t for p in phrases)


def short_block_reason(err: str | None) -> str:
    s = normalize_send_error(err or "")
    if "|" in s:
        parts = s.split("|")
        if len(parts) >= 3 and parts[2].strip():
            return parts[2].strip()[:220]
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()[:220]
    return s[:220]


async def block_control_enabled(session: AsyncSession, db_user_id: int) -> bool:
    user = (
        await session.execute(sa_select(User).where(User.id == int(db_user_id)).limit(1))
    ).scalars().first()
    if not user:
        return False
    return _truthy(await get_user_setting(session, user, "block_control"))


async def notify_smtp_stream_stopped_for_imap(
    bot: Bot,
    chat_id: int,
    account_email: str,
    *,
    reason: str | None = None,
) -> None:
    em = html.escape((account_email or "").strip())
    text = (
        f"⚡️ Поток SMTP для <code>{em}</code> завершён.\n"
        f"Оставляем ящик для IMAP (входящие)."
    )
    r = (reason or "").strip()
    if r:
        text += f"\n\n<code>{html.escape(short_block_reason(r))}</code>"
    await bot.send_message(int(chat_id), text, parse_mode="HTML")


async def mark_account_smtp_blocked(
    session: AsyncSession,
    account: EmailAccount,
    err: str,
    *,
    db_user_id: int,
    bot: Bot | None = None,
    chat_id: int | None = None,
) -> bool:
    """
    Пометить ящик smtp_blocked и (если включён контроль блокировок) уведомить в Telegram.
    Возвращает True, если это ошибка уровня аккаунта (ящик снят с SMTP).
    """
    if not is_smtp_account_block_error(err):
        return False

    was_blocked = (account.status or "").strip().lower() == "smtp_blocked"
    account.status = "smtp_blocked"
    account.last_error = (err or "")[:1000]
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    if was_blocked:
        return True

    if bot and chat_id and await block_control_enabled(session, db_user_id):
        await notify_smtp_stream_stopped_for_imap(
            bot,
            int(chat_id),
            account.email or "",
            reason=err,
        )
        em = html.escape((account.email or "").strip())
        await bot.send_message(
            int(chat_id),
            f"<b>{em}</b>: неактивен для отправок 🔴 · IMAP остаётся 🟢",
            parse_mode="HTML",
        )
    return True
