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

_ERROR_HINTS = {
    "PROXY_ERROR": "Прокси / таймаут SMTP (проверьте SOCKS5 в «Прокси»)",
    "ACCOUNT_INVALID_CREDENTIALS": "Неверный пароль почты (нужен пароль приложения)",
    "ACCOUNT_WEB_LOGIN_REQUIRED": "Gmail просит войти в браузере — разблокируйте аккаунт",
    "ACCOUNT_RATE_LIMIT": "Лимит отправки Gmail — сделайте паузу или смените аккаунт",
    "ACCOUNT_BLOCKED": "Почтовый аккаунт заблокирован для отправки",
    "RECIPIENT_DEAD": "Адрес не существует (удалён из очереди)",
    "RECIPIENT_REFUSED": "Сервер отклонил письмо на этот адрес",
    "TG_ERROR": "Сбой Telegram (сеть бота)",
    "NO_ACCOUNTS": "Нет активных аккаунтов",
}


def _humanize_send_error(raw: str) -> str:
    """Короткое описание ошибки рассылки для /stat."""
    s = (raw or "").strip()
    if not s or s == "-":
        return ""

    kind = s.split("|", 1)[0].split(":", 1)[0].strip().upper()
    hint = _ERROR_HINTS.get(kind, "")
    detail = s.replace("\n", " ").strip()
    if len(detail) > 220:
        detail = detail[:220] + "…"
    if hint:
        return f"{hint}\n<code>{detail}</code>"
    return f"<code>{detail}</code>"


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
    offers_total: int | None = None,
    pending_now: int | None = None,
    accounts_total: int | None = None,
    accounts_active: int | None = None,
) -> str:
    """Статус рассылки + данные в БД (всегда, даже если рассылка не запущена)."""
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
        last_to = (st.get("last_failed_to") or "").strip()
    elif st:
        running = bool(getattr(st, "is_running", False) or getattr(st, "running", False))
        sent = int(getattr(st, "sent_count", 0) or getattr(st, "sent", 0) or 0)
        failed = int(getattr(st, "failed_count", 0) or getattr(st, "errors", 0) or 0)
        mode = (getattr(st, "last_status", None) or "-").upper()
        acc_t = getattr(st, "accounts_total", None)
        acc_a = getattr(st, "accounts_active", None)
        last_err = (getattr(st, "last_error", "") or "").strip()
        last_to = (getattr(st, "last_failed_to", "") or "").strip()
    else:
        running = False
        sent = failed = 0
        mode = "-"
        acc_t = acc_a = None
        last_err = ""
        last_to = ""

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
    offers_total = int(offers_total or 0)

    if running:
        run_line = "🟢 Рассылка запущена"
    else:
        run_line = "Сейчас рассылка не запущена."

    last_err_line = ""
    if int(failed) > 0:
        if last_err and last_err not in ("-", ""):
            who = f" → <code>{last_to}</code>" if last_to else ""
            last_err_line = f"\n\n<b>Последняя ошибка</b>{who}\n{_humanize_send_error(last_err)}"
        else:
            last_err_line = (
                "\n\n<i>Были ошибки, но текст последней уже не в памяти — "
                "после следующей ошибки снова появится здесь.</i>"
            )

    progress_line = ""
    if running and pending_now > 0:
        progress_line = f"\nПрогресс: <b>{sent}/{pending_now}</b>"

    return (
        "📊 <b>Статус рассылки</b>\n\n"
        f"{run_line}\n"
        f"Режим: <b>{mode}</b>\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Ошибок отправки: <b>{failed}</b>"
        f"{progress_line}"
        f"{last_err_line}\n\n"
        "<b>В базе данных</b>\n"
        f"📄 Объявлений: <b>{offers_total}</b>\n"
        f"📧 Email в очереди: <b>{pending_now}</b>\n"
        f"📮 Аккаунты: <b>{acc_a}/{acc_t}</b> активных"
    )


async def _collect_db_stats(tg_user_id: int) -> tuple[int, int, int, int]:
    """(offers_total, pending_emails, accounts_total, accounts_active)"""
    async with async_session() as session:
        db_user = await get_or_create_user(session, tg_user_id)
        db_user_id = db_user.id

        offers_total = (
            await session.execute(
                select(func.count(Offer.id)).where(Offer.user_id == db_user_id)
            )
        ).scalar() or 0

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

        return (
            int(offers_total),
            int(pending_now),
            int(accounts_total),
            int(accounts_active),
        )


@router.message(Command("stat", "status", "statussend"))
@router.message(F.text == "📊 Статус рассылки")
async def cmd_statussend(message: Message) -> None:
    tg_user_id = message.from_user.id
    st = get_sending_state(tg_user_id)

    # Быстрый отклик, пока считаем БД (рассылка не блокирует, но /stat тяжёлый на SQLite).
    wait_msg = await message.answer("⏳ Считаю статистику…")

    offers_total, pending_now, acc_total, acc_active = await _collect_db_stats(tg_user_id)

    text = render_status_text(
        st,
        offers_total=offers_total,
        pending_now=pending_now,
        accounts_total=acc_total,
        accounts_active=acc_active,
    )
    try:
        await wait_msg.edit_text(text, parse_mode="HTML")
    except Exception:
        await message.answer(text, parse_mode="HTML")
