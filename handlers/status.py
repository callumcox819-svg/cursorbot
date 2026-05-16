from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from sqlalchemy import select, func

from database import async_session
from models import OfferEmail, Offer, EmailAccount
from services.users import get_or_create_user
from services.sending_state import get_sending_state, SendingState

router = Router()
logger = logging.getLogger(__name__)


def tg_answer_safe(obj: Message | CallbackQuery, text: str, **kwargs):
    """Безопасный ответ (Message -> answer, CallbackQuery -> message.answer)."""
    try:
        if isinstance(obj, CallbackQuery):
            return obj.message.answer(text, **kwargs)
        return obj.answer(text, **kwargs)
    except Exception as e:
        logger.exception("tg_answer_safe error: %s", e)
        return None


def render_status_text(
    st: SendingState | dict | None,
    *,
    pending_now: int | None = None,
    accounts_total: int | None = None,
    accounts_active: int | None = None,
) -> str:
    """Красивый статус рассылки.

    Требования:
    - показать отправлено и сколько сейчас в БД для отправки
    - показать активные аккаунты (active/total)
    """
    if not st:
        return "📊 Статус рассылки\n\nСейчас рассылка не запущена."

    # st может быть dict (на всякий)
    if isinstance(st, dict):
        # максимально мягко
        running = bool(st.get("is_running") or st.get("running"))
        sent = int(st.get("sent_count") or st.get("sent") or 0)
        failed = int(st.get("failed_count") or st.get("errors") or 0)
        mode = (st.get("last_status") or "-").upper() or "-"
        acc_t = st.get("accounts_total")
        acc_a = st.get("accounts_active")
        last_err = (st.get("last_error") or "").strip()
    else:
        running = bool(getattr(st, "is_running", False) or getattr(st, "running", False))
        sent = int(getattr(st, "sent_count", 0) or getattr(st, "sent", 0) or 0)
        failed = int(getattr(st, "failed_count", 0) or getattr(st, "errors", 0) or 0)
        mode = (getattr(st, "last_status", None) or "-").upper()
        acc_t = getattr(st, "accounts_total", None)
        acc_a = getattr(st, "accounts_active", None)
        last_err = (getattr(st, "last_error", "") or "").strip()

    # приоритет: свежие значения из БД
    if accounts_total is not None:
        acc_t = accounts_total
    if accounts_active is not None:
        acc_a = accounts_active

    acc_t = int(acc_t or 0)
    acc_a = int(acc_a or 0)

    # pending_now - сколько реально осталось в БД для отправки
    if pending_now is None:
        pending_now = int(getattr(st, "total_targets", 0) or getattr(st, "total", 0) or 0)
    pending_now = int(pending_now)

    status_line = "🟢 Рассылка запущена" if running else "🟡 Рассылка остановлена"

    # показать последнюю ошибку, если есть
    last_err_line = ""
    if last_err and last_err != "-":
        # чтобы не раздувать сообщение
        compact = last_err.replace("\n", " ").strip()
        if len(compact) > 160:
            compact = compact[:160] + "…"
        last_err_line = f"\nПоследняя ошибка: <code>{compact}</code>"

    return (
        "📊 Статус рассылки\n\n"
        f"{status_line}\n"
        f"Режим: <b>{mode}</b>\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"В очереди: <b>{pending_now}</b>\n"
        f"Прогресс: <b>{sent}/{pending_now}</b>\n"
        f"Аккаунты: <b>{acc_a}/{acc_t}</b>\n"
        f"Ошибок: <b>{failed}</b>"
        f"{last_err_line}"
    )


async def _collect_db_stats(tg_user_id: int) -> tuple[int, int, int]:
    """(pending_now, accounts_total, accounts_active)"""
    async with async_session() as session:
        db_user = await get_or_create_user(session, tg_user_id)
        db_user_id = db_user.id

        pending_now = (
            await session.execute(
                select(func.count(OfferEmail.id))
                .select_from(OfferEmail)
                .join(Offer, OfferEmail.offer_id == Offer.id)
                .where(Offer.user_id == db_user_id)
            )
        ).scalar() or 0

        accounts_total = (
            await session.execute(
                select(func.count(EmailAccount.id)).where(EmailAccount.user_id == db_user_id)
            )
        ).scalar() or 0

        accounts_active = (
            await session.execute(
                select(func.count(EmailAccount.id)).where(
                    EmailAccount.user_id == db_user_id,
                    EmailAccount.status == "active",
                )
            )
        ).scalar() or 0

        return int(pending_now), int(accounts_total), int(accounts_active)


@router.message(Command("statussend"))
@router.message(F.text == "📊 Статус рассылки")
async def cmd_statussend(message: Message) -> None:
    tg_user_id = message.from_user.id
    st = get_sending_state(tg_user_id)

    pending_now, acc_total, acc_active = await _collect_db_stats(tg_user_id)

    await message.answer(
        render_status_text(
            st,
            pending_now=pending_now,
            accounts_total=acc_total,
            accounts_active=acc_active,
        )
    )
