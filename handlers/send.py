from __future__ import annotations

import asyncio
import logging
import os
import random
import time
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
    send_mailing_one,
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

def mailing_send_timeouts(*, batch_size: int = 1) -> int:
    return mailing_send_overall_timeout_sec(batch_size=batch_size)

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


def _shuffle_rotation_accounts(accounts: List[EmailAccount]) -> List[EmailAccount]:
    out = list(accounts)
    random.shuffle(out)
    return out


def _remove_account_from_rotation(
    rotation_accounts: List[EmailAccount], account_id: int
) -> List[EmailAccount]:
    """Убрать ящик из SMTP-ротации (smtp_blocked — IMAP не трогаем)."""
    aid = int(account_id)
    return [a for a in rotation_accounts if int(a.id) != aid]


async def _get_targets(
    session: AsyncSession, user_id: int, *, limit: int | None = None
) -> List[OfferEmail]:
    """Targets are OfferEmail rows belonging to offers of this user."""
    q = (
        select(OfferEmail)
        .join(Offer, Offer.id == OfferEmail.offer_id)
        .where(Offer.user_id == user_id)
        .options(selectinload(OfferEmail.offer))
        .order_by(OfferEmail.id.asc())
    )
    if limit is not None:
        q = q.limit(max(1, int(limit)))
    rows = (await session.execute(q)).scalars().all()
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


async def clear_mailing_queue(session: AsyncSession, user_id: int) -> int:
    """
    Очистить очередь /send: все OfferEmail пользователя.
    Лоты (Offer) и raw_json в БД не трогаем — после нового JSON валидация снова наполнит очередь.
    """
    before = int(await _get_targets_count(session, user_id))
    if before <= 0:
        return 0
    await session.execute(
        delete(OfferEmail).where(
            OfferEmail.offer_id.in_(select(Offer.id).where(Offer.user_id == int(user_id)))
        )
    )
    await _safe_commit(session)
    return before


async def _build_message_for_target(
    session: AsyncSession,
    tg_user_id: int,
    tgt: OfferEmail,
    *,
    subject_rotation_index: int = 0,
) -> Tuple[str, str]:
    """Return (subject, body) for a single OfferEmail target."""

    offer: Offer | None = getattr(tgt, "offer", None)

    from services.offer_storage import offer_effective_title

    item_title = offer_effective_title(offer)
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
    from services.text_ascii import fold_plain_mail_text

    body = fold_plain_mail_text(body)

    from services.subject_offer import subject_for_offer

    subject = subject_for_offer(item_title or "", rotation_index=int(subject_rotation_index))

    return subject, body


@router.message(Command("reset"))
async def cmd_reset_queue(message: Message) -> None:
    """Очистить очередь рассылки (OfferEmail), лоты в БД остаются."""
    tg_user_id = message.from_user.id

    st = get_sending_state(tg_user_id)
    if st and st.is_running and not st.is_stopping:
        from handlers.stopsend import stop_sending_for_user

        stop_sending_for_user(tg_user_id)

    async with db_session() as session:
        user = await get_or_create_user(session, int(tg_user_id))
        removed = await clear_mailing_queue(session, int(user.id))
        offers_left = (
            await session.execute(select(func.count(Offer.id)).where(Offer.user_id == int(user.id)))
        ).scalar() or 0

    try:
        from services.mailing_active_db import set_mailing_active

        await set_mailing_active(tg_user_id, active=False)
    except Exception:
        logger.exception("reset: clear mailing_active tg=%s", tg_user_id)

    if removed <= 0:
        text = (
            "📭 <b>Очередь рассылки уже пуста</b>\n\n"
            f"Лотов в БД: <b>{offers_left}</b>\n"
            "Загрузите JSON и прогоните валидацию — адреса снова попадут в очередь."
        )
    else:
        stop_note = ""
        if st and (st.is_running or st.is_stopping):
            stop_note = "\n⏹ Активная рассылка помечена на остановку.\n"
        text = (
            f"✅ <b>Очередь рассылки очищена</b>{stop_note}\n"
            f"Удалено email из очереди: <b>{removed}</b>\n"
            f"Лотов в БД (без изменений): <b>{offers_left}</b>\n\n"
            "<i>OfferEmail сняты — /send не кому писать. "
            "Загрузите новый JSON и валидацию, чтобы собрать очередь заново.</i>"
        )

    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb(tg_user_id))


@router.message(Command("send"))
@router.message(F.text == "▶️ Запустить рассылку")
async def send_cmd(message: Message):
    await start_sending(message)


async def start_sending(message: Message):
    tg_user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    status_msg = await message.answer("⏳ Проверяю очередь и аккаунты…", parse_mode="HTML")

    async with db_session() as session:
        db_user = await get_or_create_user(session, int(tg_user_id))

        db_user_id = db_user.id

        from services.proxy_binding import ensure_all_accounts_assigned

        bound_n = await ensure_all_accounts_assigned(session, int(db_user_id))
        if bound_n:
            logger.info("Assigned proxy to %s accounts for user_id=%s", bound_n, db_user_id)

        accounts = await _get_active_accounts(session, db_user_id)

        accounts_total_db = (
            await session.execute(select(func.count(EmailAccount.id)).where(EmailAccount.user_id == db_user_id))
        ).scalar() or 0

        total_targets = await _get_targets_count(session, db_user_id)

        if not accounts:
            await status_msg.edit_text(
                "❌ Нет активных аккаунтов.\nДобавьте почту в «Настройки → Аккаунты».",
                parse_mode="HTML",
            )
            await message.answer("Меню:", reply_markup=main_menu_kb(tg_user_id))
            return

        if total_targets <= 0:
            await status_msg.edit_text(
                "❌ Очередь пуста — нет email в БД после валидации.\n"
                "Загрузите JSON и прогоните валидацию (OfferEmail).",
                parse_mode="HTML",
            )
            await message.answer("Меню:", reply_markup=main_menu_kb(tg_user_id))
            return

        state = get_sending_state(tg_user_id)
        if state and getattr(state, "is_running", False):
            await status_msg.edit_text("⚠️ Рассылка уже запущена.")
            return

        from proxy_manager import is_socks5_proxy

        all_px = (
            await session.execute(select(Proxy).where(Proxy.user_id == db_user_id))
        ).scalars().all()
        socks_total = sum(1 for p in all_px if is_socks5_proxy(p))

        if socks_total <= 0:
            await status_msg.edit_text(
                "❌ Нет SOCKS5 прокси. Добавьте socks5://… в «Прокси».",
                parse_mode="HTML",
            )
            await message.answer("Меню:", reply_markup=main_menu_kb(tg_user_id))
            return

    try:
        await status_msg.edit_text(
            "⏳ Проверяю SOCKS5 (туннель + SMTP+STARTTLS)…\n"
            "<i>Это может занять 1–2 минуты.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    from services.mailing_proxy_health import preflight_proxies_for_mailing

    px_ok, px_summary, px_detail = await preflight_proxies_for_mailing(int(db_user_id))
    if not px_ok:
        try:
            await status_msg.edit_text(
                "❌ <b>Рассылка не запущена</b>\n\n" + px_detail,
                parse_mode="HTML",
            )
        except Exception:
            await tg_answer_safe(
                message,
                "❌ Рассылка не запущена.\n\n" + px_detail,
                reply_markup=main_menu_kb(tg_user_id),
                parse_mode="HTML",
            )
        return

    sendable_px = int(px_summary.ok) + int(px_summary.unknown)

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
    from services.smtp_proxy_send import (
        MAIL_MAILING_MAX_PROXIES,
        MAIL_MAILING_TIMEOUT_SEC,
    )

    try:
        await status_msg.edit_text(
            "✅ <b>Рассылка запущена</b>\n"
            f"В очереди: <b>{total_targets}</b> · ящиков active: <b>{len(accounts)}</b>\n"
            f"{px_detail}\n"
            f"В рассылке SOCKS5: <b>{sendable_px}</b> (🔴 не используются)\n"
            f"Режим: <b>SOCKS5 → один SMTP-сеанс на пачку</b> с ящика · пауза MIN–MAX\n"
            f"Успех в /stat: <b>{'IMAP Sent' if MAIL_VERIFY_SENT else 'SMTP 250+NOOP'}</b>\n"
            f"Прокси: <b>1 на ящик</b> (привязка, без ротации) · SMTP <b>{MAIL_MAILING_TIMEOUT_SEC}</b> с\n\n"
            "<i>Пачка: ⚙️ Интервал → третье число (пример: <code>2 4 5</code>). "
            "Ящик с Message blocked снимается с рассылки.</i>",
            parse_mode="HTML",
        )
    except Exception:
        await tg_answer_safe(
            message,
            "✅ Рассылка запущена.",
            reply_markup=main_menu_kb(tg_user_id),
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
        f"Отправлено (SMTP): <b>{sent}</b>\n"
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
    account_send_counts: dict[int, int] = {}
    subject_seq = 0

    async with db_session() as session:
        user = await get_or_create_user(session, tg_user_id)
        db_user_id = user.id
        from services.subject_offer import load_subject_rotation_index

        subject_seq = await load_subject_rotation_index(session, int(db_user_id))

        rotation_accounts = _shuffle_rotation_accounts(
            await _get_active_accounts(session, db_user_id)
        )
        if not rotation_accounts:
            state.is_running = False
            state.last_error = "NO_ACCOUNTS|no_accounts|No active accounts"
            set_sending_state(tg_user_id, state=state)
            try:
                await bot.send_message(chat_id, "❌ Рассылка остановлена: нет активных аккаунтов.")
            except Exception:
                pass
            return

        from services.smtp_proxy_send import _list_active_socks5_proxies

        if not await _list_active_socks5_proxies(session, int(db_user_id)):
            state.is_running = False
            state.last_error = "PROXY_ERROR|no_active_proxy|No sendable SOCKS5"
            set_sending_state(tg_user_id, state=state)
            try:
                await bot.send_message(
                    chat_id,
                    "❌ Рассылка остановлена: нет 🟢/🟡 SOCKS5 (все 🔴). "
                    "Проверьте прокси в «Прокси».",
                )
            except Exception:
                pass
            return

    mail_max_per_account = max(0, int(os.getenv("MAIL_MAX_PER_ACCOUNT", "0")))

    try:
        while True:
            await asyncio.sleep(0)
            entered_main_loop = True
            state = get_sending_state(tg_user_id) or state
            if state.is_stopping:
                state.is_running = False
                set_sending_state(tg_user_id, state=state)
                break

            iter_started = time.monotonic()
            min_delay = 2.0
            max_delay = 4.0

            async with db_session() as session:
                user = await get_or_create_user(session, tg_user_id)
                db_user_id = int(user.id)
                sender_name = getattr(user, "sender_name", None)
                timing = await load_timing(session, tg_user_id)
                min_delay = float(timing.get("min_delay", 2.0))
                max_delay = float(timing.get("max_delay", 4.0))

                if not rotation_accounts:
                    rotation_accounts = _shuffle_rotation_accounts(
                        await _get_active_accounts(session, db_user_id)
                    )
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

                burst_size = max(1, min(8, int(timing.get("batch_size", 3))))
                targets_batch = await _get_targets(session, db_user_id, limit=burst_size)
                if not targets_batch:
                    state.is_running = False
                    state.last_status = "DONE"
                    set_sending_state(tg_user_id, state=state)
                    break

                send_one_timeout = mailing_send_timeouts(batch_size=burst_size)

                # Ротация ящиков; с ящика — пачка писем за одно SMTP через SOCKS5.
                acc: EmailAccount | None = None
                if mail_max_per_account > 0:
                    eligible = [
                        a
                        for a in rotation_accounts
                        if account_send_counts.get(int(a.id), 0) < mail_max_per_account
                    ]
                    if not eligible:
                        state.is_running = False
                        state.last_error = (
                            "ACCOUNT_RATE_LIMIT|cap|All accounts hit MAIL_MAX_PER_ACCOUNT"
                        )
                        set_sending_state(tg_user_id, state=state)
                        try:
                            await bot.send_message(
                                chat_id,
                                f"⏹ Лимит писем с ящика ({mail_max_per_account}) за этот запуск. "
                                "Запустите рассылку снова позже.",
                            )
                        except Exception:
                            pass
                        break
                    acc = eligible[acc_idx % len(eligible)]
                else:
                    acc = rotation_accounts[acc_idx % len(rotation_accounts)]
                acc_idx += 1

                batch_jobs: list[tuple[OfferEmail, str, str, str]] = []
                subj_idx = int(subject_seq)
                for tgt in targets_batch:
                    to_addr = (tgt.email or "").strip()
                    if not to_addr:
                        continue
                    subject, body = await _build_message_for_target(
                        session,
                        tg_user_id,
                        tgt,
                        subject_rotation_index=subj_idx,
                    )
                    subj_idx += 1
                    batch_jobs.append((tgt, to_addr, subject, body))

                if not batch_jobs:
                    continue

                subject_seq = subj_idx
                from services.subject_offer import save_subject_rotation_index

                await save_subject_rotation_index(session, int(db_user_id), int(subject_seq))

                state.current_to = batch_jobs[0][1]
                state.last_status = "SENDING"
                set_sending_state(tg_user_id, state=state)

                smtp_items = [(j[1], j[2], j[3]) for j in batch_jobs]
                bound_proxy = None
                if acc.proxy_id:
                    from services.proxy_binding import get_proxy_row

                    bound_proxy = await get_proxy_row(
                        session, int(acc.proxy_id), int(db_user_id)
                    )

                async def _run_batch() -> list:
                    from services.mailing_send import send_mailing_batch

                    async with smtp_sem:
                        return await asyncio.wait_for(
                            send_mailing_batch(
                                session,
                                db_user_id,
                                acc,
                                smtp_items,
                                sender_name=sender_name,
                            ),
                            timeout=send_one_timeout,
                        )

                try:
                    logger.info(
                        "[mailing burst] from=%s count=%s first=%s",
                        acc.email,
                        len(smtp_items),
                        smtp_items[0][0],
                    )
                    results = await _run_batch()
                except asyncio.TimeoutError:
                    t_err = normalize_send_error(
                        f"SMTP_TIMEOUT|timeout|batch exceeded {send_one_timeout}s"
                    )
                    from services.proxy_binding import (
                        eject_proxy_after_mailing_failure,
                        is_mailing_proxy_failure,
                    )

                    if is_mailing_proxy_failure(t_err):
                        await eject_proxy_after_mailing_failure(
                            session,
                            account=acc,
                            proxy=bound_proxy,
                            err=t_err,
                        )
                        try:
                            results = await _run_batch()
                        except asyncio.TimeoutError:
                            results = [(False, t_err)] * len(batch_jobs)
                        except Exception as e:
                            results = [(False, normalize_send_error(str(e)))] * len(
                                batch_jobs
                            )
                    else:
                        results = [(False, t_err)] * len(batch_jobs)
                except Exception as e:
                    err_one = normalize_send_error(str(e))
                    results = [(False, err_one)] * len(batch_jobs)

                if results and all(not ok for ok, _ in results):
                    from services.proxy_binding import (
                        eject_proxy_after_mailing_failure,
                        is_mailing_proxy_failure,
                    )

                    first_err = results[0][1]
                    if is_mailing_proxy_failure(first_err):
                        replaced = await eject_proxy_after_mailing_failure(
                            session,
                            account=acc,
                            proxy=bound_proxy,
                            err=first_err,
                        )
                        if replaced:
                            try:
                                logger.info(
                                    "[mailing burst retry] account=%s proxy_id=%s",
                                    acc.email,
                                    replaced.id,
                                )
                                results = await _run_batch()
                            except asyncio.TimeoutError:
                                pass
                            except Exception:
                                pass

                state.current_to = ""
                for (tgt, to_addr, subject, body), (ok, err) in zip(batch_jobs, results):
                    if ok:
                        state.sent_count += 1
                        state.last_status = "NORMAL"
                        account_send_counts[int(acc.id)] = (
                            account_send_counts.get(int(acc.id), 0) + 1
                        )
                        offer_sent = getattr(tgt, "offer", None)
                        if not offer_sent and getattr(tgt, "offer_id", None):
                            offer_sent = (
                                await session.execute(
                                    select(Offer)
                                    .where(Offer.id == int(tgt.offer_id))
                                    .where(Offer.user_id == int(db_user_id))
                                    .limit(1)
                                )
                            ).scalars().first()
                        if offer_sent:
                            from services.offer_storage import (
                                offer_effective_link,
                                offer_effective_title,
                            )
                            from services.mailing_send_log import record_mailing_send
                            from services.incoming_mail_worker import _upsert_convlink
                            from services.offer_matching import _canon_email

                            offer_link = (offer_effective_link(offer_sent) or "").strip()
                            inbox_c = _canon_email(acc.email or "")
                            contact_c = _canon_email(to_addr)
                            try:
                                await record_mailing_send(
                                    session,
                                    user_id=int(db_user_id),
                                    offer_id=int(offer_sent.id),
                                    offer_email_id=int(tgt.id),
                                    inbox_email=inbox_c,
                                    to_email=contact_c,
                                    subject=subject,
                                    title_snapshot=offer_effective_title(offer_sent),
                                    offer=offer_sent,
                                )
                                await _safe_commit(session)
                                logger.info(
                                    "mailing_sends saved offer_id=%s to=%s inbox=%s",
                                    int(offer_sent.id),
                                    contact_c,
                                    inbox_c,
                                )
                            except Exception:
                                await _safe_rollback(session)
                                logger.exception(
                                    "mailing_sends commit failed to=%s",
                                    to_addr,
                                )
                            else:
                                try:
                                    if offer_link:
                                        await _upsert_convlink(
                                            user_id=int(db_user_id),
                                            inbox_email=inbox_c,
                                            contact_email=contact_c,
                                            ad_url=offer_link,
                                            pinned_offer_id=int(offer_sent.id),
                                        )
                                except Exception:
                                    logger.exception(
                                        "convlink after send to=%s",
                                        to_addr,
                                    )
                        else:
                            logger.warning(
                                "send ok but no Offer for tgt id=%s to=%s — mailing_sends skipped",
                                int(tgt.id),
                                to_addr,
                            )
                        await _purge_target(session, db_user_id, tgt.id)
                    else:
                        state.last_status = "NORMAL"
                        blocked = await _handle_send_failure(
                            session=session,
                            db_user_id=db_user_id,
                            state=state,
                            tgt=tgt,
                            err=err or "UNKNOWN",
                            acc=acc,
                            bot=bot,
                            chat_id=chat_id,
                        )
                        if blocked:
                            rotation_accounts = _remove_account_from_rotation(
                                rotation_accounts, int(acc.id)
                            )
                            state.accounts_active = len(rotation_accounts)
                            logger.info(
                                "removed smtp_blocked account from rotation: %s",
                                acc.email,
                            )
                            break

                set_sending_state(tg_user_id, state=state)

            if not state.is_running:
                break
            # Интервал = минимум секунд на одно письмо (как в обычном софте), не «SMTP + пауза».
            pace = random.uniform(min_delay, max_delay)
            wait_more = pace - (time.monotonic() - iter_started)
            if wait_more > 0:
                await asyncio.sleep(wait_more)

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
