from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import List, Optional, Tuple

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramNetworkError

from sqlalchemy import select, func, delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import db_session
from models import EmailAccount, OfferEmail, Offer, User, Proxy

from services.mailing_send import (
    MAIL_VERIFY_SENT,
    mailing_send_overall_timeout_sec,
    send_mailing_one_verified,
)
from services.users import get_or_create_user
from services.user_settings import get_user_setting
from services.placeholders import apply_placeholders

from handlers.status import render_status_text, tg_answer_safe
from services.sender import (
    SMTP_TIMEOUT_SEC,
    normalize_send_error,
    is_smtp_timeout_error,
)
from services.smtp_block_control import mark_account_smtp_blocked
from services.smtp_account_check import is_account_no_access_error
from keyboards.main_menu import main_menu_kb

from services.sending_state import SendingState
from services.sending_state import get_state as _get_sending_state
from services.sending_state import set_state as _set_sending_state
from services.settings import load_timing
router = Router(name="send")
logger = logging.getLogger(__name__)


# ============================================================
# 🔒 Совместимость API состояния рассылки
#
# В проекте есть handlers/stopsend.py, который импортирует
# get_sending_state из handlers/send.py.
# При этом реальное хранилище состояния находится в services/sending_state.py
# и оно синхронное.
#
# Поэтому тут делаем тонкие обёртки:
# - get_sending_state(user_id) -> SendingState | None
# - set_sending_state(user_id, state=...) -> SendingState
#
# Никакой новой логики, только совместимость.
# ============================================================


def get_sending_state(user_id: int) -> Optional[SendingState]:
    return _get_sending_state(user_id)


def set_sending_state(user_id: int, state: Optional[SendingState] = None, **kwargs) -> SendingState:
    if state is not None:
        # сохранить все известные поля
        return _set_sending_state(user_id, **getattr(state, "__dict__", {}))
    return _set_sending_state(user_id, **kwargs)

# ==========================
# Константы
# ==========================

# SOCKS5 через PySocks — один глобальный lock в ProxySMTPContext; >1 только ждут в очереди.
SMTP_CONCURRENCY_WITH_PROXY = 1
SMTP_CONCURRENCY_NO_PROXY = 1

def mailing_send_timeouts() -> int:
    return mailing_send_overall_timeout_sec()

# user settings keys (уже используются в проекте)
GAG_PROFILE_NAME_KEY = "gag_profile_name"
GAG_PROFILE_ADDRESS_KEY = "gag_profile_address"


async def _safe_commit(session: AsyncSession):
    try:
        await session.commit()
    except OperationalError:
        await session.rollback()
        raise


async def _safe_rollback(session: AsyncSession):
    try:
        await session.rollback()
    except Exception:
        pass


async def _get_active_accounts(session: AsyncSession, user_id: int) -> List[EmailAccount]:
    rows = (
        await session.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == user_id,
                # В текущей модели EmailAccount нет is_active.
                # Активность аккаунта хранится в поле status (см. handlers/accounts.py).
                EmailAccount.status == "active",
            )
        )
    ).scalars().all()
    return list(rows)


async def _get_targets(session: AsyncSession, user_id: int) -> List[OfferEmail]:
    """Targets are OfferEmail rows belonging to offers of this user."""
    rows = (
        await session.execute(
            select(OfferEmail)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == user_id)
            .options(selectinload(OfferEmail.offer))
            .order_by(OfferEmail.id.asc())
        )
    ).scalars().all()
    return list(rows)


async def _get_targets_count(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(OfferEmail.id))
            .select_from(OfferEmail)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == user_id)
        )
    ).scalar() or 0


async def _purge_target(session: AsyncSession, user_id: int, offer_email_id: int):
    """Удаляем цель из очереди (чтобы больше не отправлять)."""
    try:
        await session.execute(
            delete(OfferEmail)
            .where(OfferEmail.id == offer_email_id)
            .where(OfferEmail.offer_id.in_(select(Offer.id).where(Offer.user_id == user_id)))
        )
        await _safe_commit(session)
    except Exception:
        await _safe_rollback(session)


async def _build_message_for_target(session: AsyncSession, tg_user_id: int, tgt: OfferEmail) -> Tuple[str, str]:
    """Return (subject, body) for a single OfferEmail target."""

    offer: Offer | None = getattr(tgt, "offer", None)

    item_title = (getattr(offer, "title", "") or "").strip()
    price = (getattr(offer, "price", "") or "").strip()
    link = (getattr(offer, "link", "") or "").strip()
    image_url = (getattr(offer, "photo", "") or "").strip()

    buyer_name = ""
    address = ""

    user = await get_or_create_user(session, tg_user_id)
    buyer_name = ((await get_user_setting(session, user, GAG_PROFILE_NAME_KEY)) or "").strip()
    address = ((await get_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY)) or "").strip()

    ctx = {
        "ITEM_TITLE": item_title,
        "PRICE": price,
        "BUYER_NAME": buyer_name,
        "ADDRESS": address,
        "IMAGE_URL": image_url,
    }

    # Умные пресеты (случайный текст) → иначе «Первые смс»
    base_text = ""
    try:
        from handlers.templates import pick_random_smart_preset

        base_text = await pick_random_smart_preset(tg_user_id, item_title)
    except Exception:
        base_text = ""
    if not (base_text or "").strip():
        try:
            from handlers.first_sms import pick_random_first_sms

            base_text = await pick_random_first_sms(tg_user_id, item_title)
        except Exception:
            base_text = ("Hello! Is this item still available? " + (item_title or "OFFER")).strip()

    body = apply_placeholders(base_text, link=link, ctx=ctx)

    # ==========================
    # Тема письма (глобально OFFER из config)
    # ==========================
    from services.subject_offer import subject_for_offer

    subject = subject_for_offer(item_title or "")

    return subject, body


@router.message(Command("send"))
@router.message(F.text == "▶️ Запустить рассылку")
async def send_cmd(message: Message):
    await start_sending(message)


async def start_sending(message: Message):
    tg_user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await tg_answer_safe(message, "⏳ Проверяю очередь и аккаунты…")

    async with db_session() as session:
        db_user = await get_or_create_user(session, int(tg_user_id))
        timing = await load_timing(session, tg_user_id)

        db_user_id = db_user.id
        accounts = await _get_active_accounts(session, db_user_id)

        accounts_total_db = (
            await session.execute(select(func.count(EmailAccount.id)).where(EmailAccount.user_id == db_user_id))
        ).scalar() or 0

        total_targets = await _get_targets_count(session, db_user_id)

        if not accounts:
            await tg_answer_safe(
                message,
                "❌ Нет активных аккаунтов.\nДобавьте почту в «Настройки → Аккаунты».",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

        if total_targets <= 0:
            await tg_answer_safe(
                message,
                "❌ Очередь пуста — нет email в БД после валидации.",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

        state = get_sending_state(tg_user_id)
        if state and getattr(state, "is_running", False):
            await tg_answer_safe(message, "⚠️ Рассылка уже запущена.")
            return

        from proxy_manager import is_socks5_proxy

        all_px = (
            await session.execute(select(Proxy).where(Proxy.user_id == db_user_id))
        ).scalars().all()
        socks_proxies = [p for p in all_px if is_socks5_proxy(p)]

        if not socks_proxies:
            await tg_answer_safe(
                message,
                "❌ Нет SOCKS5 прокси. Добавьте socks5://… в «Прокси».",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

    from services.mailing_proxy_health import (
        MAIL_PROXY_PREFLIGHT_TIMEOUT,
        MAIL_PROXY_RECHECK_SEC,
        preflight_proxies_for_mailing,
    )

    await tg_answer_safe(
        message,
        f"⏳ <b>Проверяю {len(socks_proxies)} прокси</b> (SOCKS5 → smtp.gmail.com:587)…\n"
        f"<i>До ~{max(30, len(socks_proxies) * MAIL_PROXY_PREFLIGHT_TIMEOUT // 2)} с</i>",
        parse_mode="HTML",
    )

    can_start, _summary, proxy_detail = await preflight_proxies_for_mailing(int(db_user_id))
    if not can_start:
        await tg_answer_safe(
            message,
            proxy_detail,
            reply_markup=main_menu_kb(tg_user_id),
            parse_mode="HTML",
        )
        return

    async with db_session() as session:
        state = SendingState(
            user_id=tg_user_id,
            is_running=True,
            is_stopping=False,
            total_targets=total_targets,
            sent_count=0,
            failed_count=0,
            accounts_total=int(accounts_total_db),
            accounts_active=len(accounts),
            last_error="",
            last_status="NORMAL",
        )
        set_sending_state(tg_user_id, state=state)

    from services.mailing_active_db import set_mailing_active

    await set_mailing_active(tg_user_id, active=True)

    from services.mailing_send import MAIL_SEND_RETRIES, MAIL_VERIFY_SENT

    await tg_answer_safe(
        message,
        "✅ Рассылка запущена.\n"
        f"В очереди: <b>{total_targets}</b> email\n"
        f"{proxy_detail}\n"
        f"Режим: <b>1 ящик → 1 пресет → 1 адрес</b> (ротация)\n"
        f"Учёт в /stat: <b>{'Sent (IMAP)' if MAIL_VERIFY_SENT else 'SMTP 250'}</b> · "
        f"до <b>{MAIL_SEND_RETRIES}</b> попыток на адрес\n"
        f"<i>Прокси перепроверяются каждые {max(1, MAIL_PROXY_RECHECK_SEC // 60)} мин.</i>",
        reply_markup=main_menu_kb(tg_user_id),
        parse_mode="HTML",
    )

    from services.mailing_proxy_health import mailing_proxy_watch_loop
    from utils.bg_jobs import start as bg_start

    bg_start(
        tg_user_id,
        "mailing_proxy_watch",
        mailing_proxy_watch_loop(
            tg_user_id=tg_user_id,
            db_user_id=int(db_user_id),
            bot=bot,
            chat_id=chat_id,
        ),
    )

    asyncio.create_task(
        _sending_loop(bot=bot, chat_id=chat_id, tg_user_id=tg_user_id)
    )


async def _notify_sending_finished(*, bot: Bot, chat_id: int, tg_user_id: int) -> None:
    """Отдельное сообщение по завершении рассылки (успех / стоп / сбой)."""
    state = get_sending_state(tg_user_id)
    if not state:
        return

    pending = 0
    try:
        async with db_session() as session:
            user = await get_or_create_user(session, tg_user_id)
            pending = int(await _get_targets_count(session, int(user.id)))
    except Exception:
        pass

    sent = int(state.sent_count)
    failed = int(state.failed_count)
    status = (state.last_status or "").upper()

    if state.is_stopping:
        title = "⏹ <b>Рассылка остановлена</b>"
    elif status == "DONE":
        title = "✅ <b>Рассылка завершена</b>"
    else:
        title = "⚠️ <b>Рассылка прервана</b>"

    text = (
        f"{title}\n\n"
        f"Подтверждено отправок: <b>{sent}</b>\n"
        f"Ошибок отправки: <b>{failed}</b>\n"
        f"Email в очереди: <b>{pending}</b>\n\n"
        f"<i>Если ответов мало — проверьте прокси (SMTP+STARTTLS OK) и задержку 2–5 с.</i>"
    )
    if failed > 0 and (state.last_error or "").strip() not in ("", "-"):
        who = f"\nПоследний адрес: <code>{state.last_failed_to}</code>" if state.last_failed_to else ""
        from handlers.status import _humanize_send_error

        text += f"\n\n{_humanize_send_error(normalize_send_error(state.last_error))}{who}"

    try:
        await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=main_menu_kb(tg_user_id),
        )
    except Exception:
        logger.exception("failed to send mailing finished notification user=%s", tg_user_id)


async def _handle_send_failure(
    *,
    session: AsyncSession,
    db_user_id: int,
    state: SendingState,
    tgt: OfferEmail,
    err: str,
    acc: EmailAccount,
    bot: Bot | None = None,
    chat_id: int | None = None,
) -> bool:
    """Возвращает True, если ящик снят с SMTP (smtp_blocked) — убрать из ротации."""
    err = normalize_send_error(err)
    state.failed_count += 1
    state.last_error = err or "UNKNOWN"
    state.last_failed_to = (tgt.email or "").strip()

    if await mark_account_smtp_blocked(
        session,
        acc,
        err,
        db_user_id=db_user_id,
        bot=bot,
        chat_id=chat_id,
    ):
        return True

    if is_account_no_access_error(err):
        try:
            await session.delete(acc)
            await session.commit()
            logger.warning("Deleted account (no access): %s", acc.email)
        except Exception:
            await session.rollback()
        return True

  # Ошибки прокси/таймаут — адрес остаётся в очереди (повтор на следующих кругах).
    if "RECIPIENT_DEAD" in err or "5.1.1" in err:
        await _purge_target(session, db_user_id, int(tgt.id))
    return False


async def _sending_loop(*, bot: Bot, chat_id: int, tg_user_id: int) -> None:
    state = get_sending_state(tg_user_id) or SendingState(user_id=tg_user_id)
    smtp_sem = asyncio.Semaphore(SMTP_CONCURRENCY_WITH_PROXY)
    entered_main_loop = False
    acc_idx = 0
    rotation_accounts: List[EmailAccount] = []

    async with db_session() as session:
        user = await get_or_create_user(session, tg_user_id)
        db_user_id = user.id

        rotation_accounts = list(await _get_active_accounts(session, db_user_id))
        if not rotation_accounts:
            state.is_running = False
            state.last_error = "NO_ACCOUNTS|no_accounts|No active accounts"
            set_sending_state(tg_user_id, state=state)
            try:
                await bot.send_message(chat_id, "❌ Рассылка остановлена: нет активных аккаунтов.")
            except Exception:
                pass
            return
        random.shuffle(rotation_accounts)

        from proxy_manager import is_socks5_proxy

        all_proxies = (
            await session.execute(select(Proxy).where(Proxy.user_id == int(db_user_id)))
        ).scalars().all()
        socks_n = sum(1 for p in all_proxies if is_socks5_proxy(p))
        if socks_n <= 0:
            state.is_running = False
            state.last_error = "PROXY_ERROR|no_active_proxy|No SOCKS5 configured"
            set_sending_state(tg_user_id, state=state)
            try:
                await bot.send_message(
                    chat_id,
                    "❌ Рассылка остановлена: нет SOCKS5 в «Прокси».",
                )
            except Exception:
                pass
            return

        timing = await load_timing(session, tg_user_id)
        min_delay = float(timing.get("min_delay", 2.0))
        max_delay = float(timing.get("max_delay", 4.0))
    send_one_timeout = mailing_send_timeouts()

    try:
        while True:
            await asyncio.sleep(0)
            entered_main_loop = True
            state = get_sending_state(tg_user_id) or state
            if state.is_stopping:
                state.is_running = False
                set_sending_state(tg_user_id, state=state)
                break

            async with db_session() as session:
                user = await get_or_create_user(session, tg_user_id)
                db_user_id = int(user.id)
                sender_name = getattr(user, "sender_name", None)

                if not rotation_accounts:
                    rotation_accounts = list(await _get_active_accounts(session, db_user_id))
                    if rotation_accounts:
                        random.shuffle(rotation_accounts)
                if not rotation_accounts:
                    state.is_running = False
                    state.last_status = "STOPPED"
                    set_sending_state(tg_user_id, state=state)
                    break

                remaining = await _get_targets_count(session, db_user_id)
                if remaining <= 0:
                    state.is_running = False
                    state.last_status = "DONE"
                    set_sending_state(tg_user_id, state=state)
                    break

                targets = await _get_targets(session, db_user_id)
                if not targets:
                    state.is_running = False
                    state.last_status = "DONE"
                    set_sending_state(tg_user_id, state=state)
                    break

                # Ротация: каждый шаг — другой активный ящик, новый случайный пресет, один получатель.
                acc = rotation_accounts[acc_idx % len(rotation_accounts)]
                acc_idx += 1
                tgt = targets[0]

                try:
                    subject, body = await _build_message_for_target(session, tg_user_id, tgt)
                    to_addr = (tgt.email or "").strip()
                    logger.info(
                        "[mailing rotate] from=%s to=%s subject=%r",
                        acc.email,
                        to_addr,
                        (subject or "")[:60],
                    )
                    async with smtp_sem:
                        ok, err, _msgid = await asyncio.wait_for(
                            send_mailing_one_verified(
                                session,
                                db_user_id,
                                acc,
                                to_addr,
                                subject,
                                body,
                                sender_name=sender_name,
                            ),
                            timeout=send_one_timeout,
                        )
                except asyncio.TimeoutError:
                    ok, err = False, normalize_send_error(
                        f"SMTP_TIMEOUT|timeout|SMTP send exceeded {send_one_timeout}s"
                    )
                except Exception as e:
                    ok, err = False, normalize_send_error(str(e))

                if ok:
                    state.sent_count += 1
                    await _purge_target(session, db_user_id, tgt.id)
                else:
                    await _handle_send_failure(
                        session=session,
                        db_user_id=db_user_id,
                        state=state,
                        tgt=tgt,
                        err=err or "UNKNOWN",
                        acc=acc,
                        bot=bot,
                        chat_id=chat_id,
                    )

            if not state.is_running:
                break

            set_sending_state(tg_user_id, state=state)
            await asyncio.sleep(random.uniform(min_delay, max_delay))

    except TelegramNetworkError:
        state.is_running = False
        state.last_error = "TG_ERROR|network|Telegram network error"
        set_sending_state(tg_user_id, state=state)
    except Exception as e:
        state.is_running = False
        state.last_error = normalize_send_error(str(e))
        set_sending_state(tg_user_id, state=state)
        logger.exception("sending loop failed for user %s", tg_user_id)
    finally:
        state = get_sending_state(tg_user_id) or state
        if state.is_running:
            state.is_running = False
            set_sending_state(tg_user_id, state=state)
        try:
            from services.mailing_active_db import set_mailing_active

            await set_mailing_active(tg_user_id, active=False)
        except Exception:
            logger.exception("clear mailing_active flag tg=%s", tg_user_id)
        if entered_main_loop:
            await _notify_sending_finished(bot=bot, chat_id=chat_id, tg_user_id=tg_user_id)
