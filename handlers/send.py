from __future__ import annotations

import asyncio
import random
from typing import List, Optional, Tuple

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramNetworkError

from sqlalchemy import select, func, delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import async_session
from models import EmailAccount, OfferEmail, Offer, User, Proxy

from services.smtp_proxy_send import (
    send_batch_via_account_with_proxy,
    send_email_via_account_with_proxy,
)
from services.users import get_or_create_user
from services.user_settings import get_user_setting
from services.placeholders import apply_placeholders

from handlers.status import render_status_text, tg_answer_safe
from keyboards.main_menu import main_menu_kb

from services.sending_state import SendingState
from services.sending_state import get_state as _get_sending_state
from services.sending_state import set_state as _set_sending_state
from services.settings import load_timing
from services.proxy_manager import choose_proxy_for_user
from services.proxy_autochecker import autocheck_proxies

router = Router(name="send")


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

SMTP_CONCURRENCY_WITH_PROXY = 2
SMTP_CONCURRENCY_NO_PROXY = 1

SEND_ONE_TIMEOUT = 25
SEND_BATCH_TIMEOUT = 60

# user settings keys (уже используются в проекте)
COUNTRY_KEY = "country"
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
    country = ((await get_user_setting(session, user, COUNTRY_KEY)) or "").strip().upper()

    # CH profile (как в авто-ответах)
    if country == "CH":
        buyer_name = ((await get_user_setting(session, user, GAG_PROFILE_NAME_KEY)) or "").strip()
        address = ((await get_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY)) or "").strip()

    ctx = {
        "ITEM_TITLE": item_title,
        "PRICE": price,
        "BUYER_NAME": buyer_name,
        "ADDRESS": address,
        "IMAGE_URL": image_url,
    }

    # "Первые смс" + spintax + OFFER
    try:
        from handlers.first_sms import pick_random_first_sms

        base_text = pick_random_first_sms(tg_user_id, item_title)
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


async def start_sending(message: Message, fast: bool = False):
    tg_user_id = message.from_user.id
    await tg_answer_safe(message, "🚀 Запускаю рассылку…")

    async with async_session() as session:
        db_user = await get_or_create_user(session, tg_user_id)
        timing = await load_timing(session, tg_user_id)

        # ✅ Fast режим должен реально включаться от кнопки "Fast режим" в настройках.
        # В меню это user_setting "fast_send", а send.py исторически читает timings_json.fast_mode.
        fast_send_toggle = str(await get_user_setting(session, db_user, "fast_send") or "").strip().lower() in {"1", "true", "yes", "on"}
        mode_fast = bool(timing.get("fast_mode", False)) or fast_send_toggle or fast

        db_user_id = db_user.id
        sender_name = getattr(db_user, "sender_name", None)

        accounts = await _get_active_accounts(session, db_user_id)

        accounts_total_db = (
            await session.execute(select(func.count(EmailAccount.id)).where(EmailAccount.user_id == db_user_id))
        ).scalar() or 0

        total_targets = await _get_targets_count(session, db_user_id)

        if not accounts:
            await tg_answer_safe(
                message,
                "❌ Валидных почт для рассылки нет.\nДобавьте и активируйте хотя бы один аккаунт в «Настройки → Аккаунты».",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

        if total_targets <= 0:
            await tg_answer_safe(
                message,
                "❌ Email для рассылки не найдены (очередь пуста).",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

        state = get_sending_state(tg_user_id)
        if state and getattr(state, "is_running", False):
            await tg_answer_safe(message, "⚠️ Рассылка уже запущена.")
            return

        proxies_total = (
            await session.execute(select(func.count(Proxy.id)).where(Proxy.user_id == db_user_id))
        ).scalar() or 0

        # ✅ Без прокси рассылку запускать нельзя
        if proxies_total <= 0:
            await tg_answer_safe(
                message,
                "❌ Рассылка без прокси запрещена.\n"
                "Зайдите в «Прокси» и добавьте хотя бы один рабочий прокси, затем запустите рассылку снова.",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

        # ✅ Авто-проверка прокси перед стартом (как в референс-боте из видео)
        # Если прокси невалидные — удаляем их из БД, чтобы не мешали рассылке.
        try:
            proxies = (
                await session.execute(select(Proxy).where(Proxy.user_id == db_user_id))
            ).scalars().all()

            def _to_url(p: Proxy) -> str:
                kind = (p.type or "http").strip().lower() or "http"
                auth = ""
                if (p.username or "") and (p.password or ""):
                    auth = f"{p.username}:{p.password}@"
                return f"{kind}://{auth}{p.host}:{int(p.port)}"

            urls = [_to_url(p) for p in proxies if p and p.host and p.port]
            if urls:
                results = await autocheck_proxies(urls, concurrency=20, timeout=10)

                bad_set = {r.proxy for r in results if not r.ok}
                if bad_set:
                    bad_ids: list[int] = []
                    for p in proxies:
                        if _to_url(p) in bad_set:
                            bad_ids.append(int(p.id))

                    if bad_ids:
                        await session.execute(delete(Proxy).where(Proxy.user_id == db_user_id, Proxy.id.in_(bad_ids)))
                        await session.commit()

                    await tg_answer_safe(message, "✅ Невалидные прокси были автоматически удалены")
                else:
                    await tg_answer_safe(message, "✅ Все прокси валидны")
        except Exception:
            # авто-чек не должен ломать запуск рассылки
            pass

        proxy_in_use = await choose_proxy_for_user(session, db_user_id)
        if not proxy_in_use:
            await tg_answer_safe(
                message,
                "❌ У вас настроены прокси, но нет активных прокси (is_active=True).\n"
                "Зайдите в «Прокси» и активируйте хотя бы один прокси.",
                reply_markup=main_menu_kb(tg_user_id),
            )
            return

        smtp_sem = asyncio.Semaphore(SMTP_CONCURRENCY_WITH_PROXY)

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
            last_status="FAST" if mode_fast else "NORMAL",
        )
        set_sending_state(tg_user_id, state=state)

    await tg_answer_safe(
        message,
        f"✅ Рассылка запущена ({'FAST' if mode_fast else 'NORMAL'}).",
        reply_markup=main_menu_kb(tg_user_id),
    )

    async def _sending_loop():
        nonlocal state

        async with async_session() as session:
            user = await get_or_create_user(session, tg_user_id)
            db_user_id = user.id
            sender_name = getattr(user, "sender_name", None)

            accounts = await _get_active_accounts(session, db_user_id)
            if not accounts:
                state.is_running = False
                state.last_error = "NO_ACCOUNTS|no_accounts|No active accounts"
                set_sending_state(tg_user_id, state=state)
                return

            timing = await load_timing(session, tg_user_id)
            min_delay = float(timing.get("min_delay", 1.5))
            max_delay = float(timing.get("max_delay", 3.0))
            batch_size = int(timing.get("batch_size", 1))
            # ✅ Fast режим: поддерживаем и timings_json.fast_mode, и переключатель fast_send.
            try:
                fast_send_toggle = str(await get_user_setting(session, user, "fast_send") or "").strip().lower() in {"1", "true", "yes", "on"}
            except Exception:
                fast_send_toggle = False
            fast_mode = bool(timing.get("fast_mode", False)) or fast_send_toggle

            if fast_mode:
                # ✅ ТЗ: Fast режим должен быть реально быстрым.
                # Не меняем общую логику рассылки, только уменьшаем задержки и увеличиваем размер пачки.
                min_delay = 0.0
                max_delay = 0.0
                batch_size = max(batch_size, 5)

            try:
                proxies_total = (
                    await session.execute(select(func.count(Proxy.id)).where(Proxy.user_id == db_user_id))
                ).scalar() or 0

                if proxies_total <= 0:
                    state.is_running = False
                    state.last_error = "PROXY_ERROR|no_proxy|No proxies configured"
                    set_sending_state(tg_user_id, state=state)
                    return

                proxy_check = await choose_proxy_for_user(session, db_user_id)
                if not proxy_check:
                    state.is_running = False
                    state.last_error = "PROXY_ERROR|no_active_proxy|No active proxy"
                    set_sending_state(tg_user_id, state=state)
                    return

                async def _send_one_with_timeout(acc: EmailAccount, tgt: OfferEmail) -> Tuple[bool, str]:
                    try:
                        subject, body = await _build_message_for_target(session, tg_user_id, tgt)
                        to_addr = (tgt.email or "").strip()

                        async with smtp_sem:
                            ok, err = await asyncio.wait_for(
                                send_email_via_account_with_proxy(
                                    session,
                                    db_user_id,
                                    acc,
                                    to_addr,
                                    subject,
                                    body,
                                    sender_name=sender_name,
                                ),
                                timeout=SEND_ONE_TIMEOUT,
                            )
                        return bool(ok), (err or "")

                    except asyncio.TimeoutError:
                        return False, "PROXY_ERROR|timeout|SMTP timeout (send_one)"
                    except Exception as e:
                        return False, str(e)

                async def _send_batch_with_timeout(acc: EmailAccount, batch: List[OfferEmail]) -> List[Tuple[bool, str]]:
                    try:
                        items: list[tuple[str, str, str]] = []
                        for t in batch:
                            subj, body = await _build_message_for_target(session, tg_user_id, t)
                            items.append(((t.email or "").strip(), subj, body))

                        async with smtp_sem:
                            res = await asyncio.wait_for(
                                send_batch_via_account_with_proxy(
                                    session,
                                    db_user_id,
                                    acc,
                                    items,
                                    sender_name=sender_name,
                                ),
                                timeout=SEND_BATCH_TIMEOUT,
                            )

                        out: List[Tuple[bool, str]] = []
                        for ok, err in res:
                            out.append((bool(ok), (err or "")))
                        return out

                    except asyncio.TimeoutError:
                        return [(False, "PROXY_ERROR|timeout|SMTP timeout (send_batch)") for _ in batch]
                    except Exception as e:
                        return [(False, str(e)) for _ in batch]

                acc_idx = 0

                while True:
                    state = get_sending_state(tg_user_id) or state
                    if state.is_stopping:
                        state.is_running = False
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

                    acc = accounts[acc_idx % len(accounts)]
                    acc_idx += 1

                    if batch_size <= 1:
                        tgt = targets[0]
                        ok, err = await _send_one_with_timeout(acc, tgt)
                        if ok:
                            state.sent_count += 1
                            state.last_error = ""
                            await _purge_target(session, db_user_id, tgt.id)
                        else:
                            state.failed_count += 1
                            state.last_error = err
                            # если получатель "мертвый" — убираем из очереди, чтобы не стопорить
                            if "RECIPIENT_DEAD" in err or "5.1.1" in err:
                                await _purge_target(session, db_user_id, tgt.id)

                    else:
                        batch = targets[:batch_size]
                        results = await _send_batch_with_timeout(acc, batch)
                        for t, (ok, err) in zip(batch, results):
                            if ok:
                                state.sent_count += 1
                                await _purge_target(session, db_user_id, t.id)
                            else:
                                state.failed_count += 1
                                state.last_error = err
                                if "RECIPIENT_DEAD" in err or "5.1.1" in err:
                                    await _purge_target(session, db_user_id, t.id)

                    set_sending_state(tg_user_id, state=state)
                    await asyncio.sleep(random.uniform(min_delay, max_delay))

            except TelegramNetworkError:
                state.is_running = False
                state.last_error = "TG_ERROR|network|Telegram network error"
                set_sending_state(tg_user_id, state=state)
            except Exception as e:
                state.is_running = False
                state.last_error = f"ERROR|exception|{e}"
                set_sending_state(tg_user_id, state=state)

    asyncio.create_task(_sending_loop())
