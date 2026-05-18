from __future__ import annotations

import asyncio
import logging
import os
import re
import imaplib
from dataclasses import dataclass
from typing import List, Optional, Tuple

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, update, or_

from database import Session, db_session
from models import User, EmailAccount
from keyboards.main_menu import main_menu_kb
from keyboards.settings_menu import settings_menu
from utils.bg_jobs import is_running as bg_is_running, start as bg_start

logger = logging.getLogger(__name__)
router = Router()

# ===========================
# Handlers: quick entry from main menu
# ===========================

@router.message(F.text.in_({"📬 Мои аккаунты", "📬 Почтовые аккаунты"}))
async def open_accounts_from_main_menu(message: Message) -> None:
    await render_accounts_menu(message, message.from_user.id, page=1, status_filter="all")

# ===========================
# States
# ===========================

class AccountsAddStates(StatesGroup):
    waiting_for_accounts_input = State()


class AccountsQuickGmailStates(StatesGroup):
    waiting_sender_name = State()
    waiting_gmail_creds = State()

# ===========================
# Helpers
# ===========================

IMAP_SERVERS: dict[str, Tuple[str, str]] = {
    "gmail.com": ("imap.gmail.com", "gmail"),
    "googlemail.com": ("imap.gmail.com", "gmail"),
    "gmx.com": ("imap.gmx.com", "gmx"),
    "gmx.net": ("imap.gmx.com", "gmx"),
    "icloud.com": ("imap.mail.me.com", "icloud"),
}

PAGE_SIZE = 10  # как на скрине

async def get_user(session: Session, telegram_id: int) -> Optional[User]:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()

def detect_imap_server(email: str) -> Tuple[str, str]:
    match = re.search(r"@([^@]+)$", email)
    if not match:
        raise ValueError("Некорректный email")
    domain = match.group(1).lower()
    if domain in IMAP_SERVERS:
        return IMAP_SERVERS[domain]
    raise ValueError(f"Неизвестный домен: {domain}")

def check_imap_credentials(email: str, password: str) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        host, provider = detect_imap_server(email)
    except ValueError as e:
        return False, None, str(e)

    try:
        with imaplib.IMAP4_SSL(host) as imap:
            imap.login(email, password)
        return True, provider, None
    except Exception as e:  # noqa: BLE001
        logger.warning("IMAP login failed for %s: %s", email, e)
        return False, provider, str(e)


async def _imap_check_async(email: str, password: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """IMAP-проверка в отдельном потоке, чтобы не блокировать polling."""
    return await asyncio.to_thread(check_imap_credentials, email, password)


async def _edit_add_progress(
    status_msg: Message,
    *,
    current: int,
    total: int,
    ok: int,
    fail: int,
    workers: int = 1,
    elapsed_sec: float | None = None,
) -> None:
    speed = ""
    if elapsed_sec and elapsed_sec > 0 and current > 0:
        per_min = current / elapsed_sec * 60.0
        speed = f"\n⚡ ~<b>{per_min:.1f}</b> акк./мин ({workers} потоков)"
    try:
        await status_msg.edit_text(
            "⏳ <b>Добавление аккаунтов</b>\n\n"
            f"Проверка IMAP: <b>{current}/{total}</b>\n"
            f"✅ успешно: <b>{ok}</b> · ❌ ошибки: <b>{fail}</b>"
            f"{speed}",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass


@dataclass
class _AccountLineWork:
    line: str
    email: str
    password: str


@dataclass
class _AccountCheckResult:
    work: _AccountLineWork | None
    fail_detail: str | None = None
    ok: bool = False
    provider: Optional[str] = None
    err: Optional[str] = None


def _trim_details(details: List[str], limit: int = 35) -> str:
    if len(details) <= limit:
        return "\n".join(details)
    hidden = len(details) - limit
    return "\n".join(details[:limit]) + f"\n… и ещё {hidden} строк(и)"


async def _bulk_add_accounts(
    message: Message,
    session,
    user: User,
    lines: List[str],
    *,
    gmail_only: bool = False,
) -> Tuple[int, int, List[str]]:
    total = len(lines)
    workers = max(1, min(8, int(os.getenv("ACCOUNTS_IMAP_CONCURRENCY", "4"))))
    status_msg = await message.answer(
        "⏳ <b>Добавление аккаунтов</b>\n\n"
        f"Проверка IMAP: <b>0/{total}</b>\n"
        f"Параллельно: <b>{workers}</b> потоков",
        parse_mode="HTML",
    )

    ok_count = 0
    fail_count = 0
    details: List[str] = []
    to_check: List[_AccountLineWork] = []

    for line in lines:
        if ":" not in line:
            fail_count += 1
            if gmail_only:
                details.append(f"❌ <code>{_e(line)}</code> — нет <code>:</code>")
            else:
                details.append(f"❌ <code>{_e(line)}</code> — нет разделителя <code>:</code>")
            continue

        email, password = line.split(":", 1)
        email = email.strip()
        password = password.strip()

        if gmail_only and not _is_gmail_address(email):
            fail_count += 1
            details.append(f"❌ <code>{_e(email)}</code> — только @gmail.com / @googlemail.com")
            continue

        if not email or not password:
            fail_count += 1
            details.append(f"❌ <code>{_e(line)}</code> — пустой email или пароль")
            continue

        to_check.append(_AccountLineWork(line=line, email=email, password=password))

    check_total = len(to_check)
    if not check_total:
        try:
            await status_msg.delete()
        except Exception:
            pass
        return ok_count, fail_count, details

    sem = asyncio.Semaphore(workers)
    done_checks = 0
    t0 = asyncio.get_running_loop().time()

    async def _run_imap(work: _AccountLineWork) -> _AccountCheckResult:
        async with sem:
            ok, provider, err = await _imap_check_async(work.email, work.password)
        return _AccountCheckResult(work=work, ok=ok, provider=provider, err=err)

    tasks = [asyncio.create_task(_run_imap(w)) for w in to_check]
    for fut in asyncio.as_completed(tasks):
        res = await fut
        done_checks += 1
        elapsed = asyncio.get_running_loop().time() - t0

        if not res.ok:
            fail_count += 1
            err_txt = _e(res.err or ("ошибка IMAP" if gmail_only else "ошибка при входе"))
            details.append(f"❌ <code>{_e(res.work.email)}</code> — {err_txt}")
        else:
            work = res.work
            existing_res = await session.execute(
                select(EmailAccount).where(
                    EmailAccount.user_id == user.id,
                    EmailAccount.email == work.email,
                )
            )
            existing = existing_res.scalar_one_or_none()
            prov = res.provider or "gmail"
            if existing:
                existing.password = work.password
                existing.provider = prov
                existing.status = "active"
            else:
                session.add(
                    EmailAccount(
                        user_id=user.id,
                        email=work.email,
                        password=work.password,
                        provider=prov,
                        status="active",
                    )
                )
            ok_count += 1
            if gmail_only:
                details.append(f"✅ <code>{_e(work.email)}</code>")
            else:
                details.append(f"✅ <code>{_e(work.email)}</code> — добавлен ({_e(prov)})")

        await _edit_add_progress(
            status_msg,
            current=done_checks,
            total=check_total,
            ok=ok_count,
            fail=fail_count,
            workers=workers,
            elapsed_sec=elapsed,
        )

    try:
        await status_msg.delete()
    except Exception:
        pass

    return ok_count, fail_count, details


def _filtered(accounts: List[EmailAccount], status_filter: str) -> List[EmailAccount]:
    sf = (status_filter or "all").lower().strip()
    if sf == "active":
        return [a for a in accounts if a.status == "active"]
    if sf in ("bad", "problem", "problematic"):
        return [a for a in accounts if a.status != "active"]
    return accounts

def accounts_menu_kb(
    accounts_page: List[EmailAccount],
    page: int,
    total_pages: int,
    status_filter: str,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []

    # Список аккаунтов
    for acc in accounts_page:
        st = (acc.status or "").strip().lower()
        if st == "active":
            emoji = "🟢"
        elif st == "smtp_blocked":
            emoji = "🟡"
        else:
            emoji = "🔴"
        text = f"{emoji} {acc.email}"

        email_btn = InlineKeyboardButton(
            text=text,
            callback_data=f"acc_info:{acc.id}:{page}:{status_filter}",
        )

        delete_btn = InlineKeyboardButton(
            text="🗑",
            callback_data=f"acc_del:{acc.id}:{page}:{status_filter}",
        )
        rows.append([email_btn, delete_btn])

    # Навигация страниц
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"acc_page:{page-1}:{status_filter}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"acc_page:{page+1}:{status_filter}"))
    rows.append(nav)

    rows.append(
        [InlineKeyboardButton(text="🗑 Удалить все неактивные", callback_data="acc_delete_inactive")]
    )
    rows.append([InlineKeyboardButton(text="🗑 Удалить все почты", callback_data="acc_delete_all")])
    rows.append(
        [InlineKeyboardButton(text="🔍 Проверить статус почт", callback_data="acc_check_smtp")]
    )

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

async def render_accounts_menu(message_or_cb, telegram_id: int, page: int = 1, status_filter: str = "all") -> None:
    async with db_session() as session:
        user = await get_user(session, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        result = await session.execute(
            select(EmailAccount)
            .where(EmailAccount.user_id == user.id)
            .order_by(EmailAccount.id)
        )
        all_accounts: List[EmailAccount] = list(result.scalars())

    accs = _filtered(all_accounts, status_filter)

    total = len(accs)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_accounts = accs[start:end]

    # Текст как на скрине
    if total:
        text = (
            "📬 <b>Настройки почтовых аккаунтов</b>\n\n"
            f"Текущие аккаунты: <b>{total}</b> шт.\n\n"
            "Выберите действие:"
        )
    else:
        text = (
            "📬 <b>Почтовые аккаунты</b>\n\n"
            "У тебя пока нет добавленных аккаунтов.\n"
            "Формат для импорта: <code>email:app_password</code> (APP PASSWORD).\n"
            "Поддерживаются: Gmail, iCloud, GMX."
        )

    kb = accounts_menu_kb(page_accounts, page, total_pages, status_filter)

    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_cb.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )

# ===========================
# Handlers: accounts menu
# ===========================

@router.callback_query(F.data == "settings_accounts")
async def open_accounts_from_settings(callback: CallbackQuery) -> None:
    await render_accounts_menu(callback, callback.from_user.id, page=1, status_filter="all")

@router.callback_query(F.data.startswith("acc_page:"))
async def acc_page(callback: CallbackQuery) -> None:
    try:
        _, page_str, status_filter = (callback.data or "").split(":", 2)
        page = int(page_str)
    except Exception:
        return await callback.answer("Ошибка страницы", show_alert=True)

    await render_accounts_menu(callback, callback.from_user.id, page=page, status_filter=status_filter)
    await callback.answer()

@router.callback_query(F.data.startswith("acc_filter:"))
async def acc_filter(callback: CallbackQuery) -> None:
    # цикл фильтра: all -> active -> bad -> all
    try:
        _, cur, page_str = (callback.data or "").split(":", 2)
        page = int(page_str)
    except Exception:
        cur, page = "all", 1

    cur = (cur or "all").lower().strip()
    if cur == "all":
        nxt = "active"
    elif cur == "active":
        nxt = "bad"
    else:
        nxt = "all"

    await render_accounts_menu(callback, callback.from_user.id, page=1, status_filter=nxt)
    await callback.answer(f"Фильтр: {nxt}")

@router.callback_query(F.data == "acc_delete_inactive")
async def acc_delete_inactive(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        if not user:
            return await callback.answer("Пользователь не найден", show_alert=True)

        result = await session.execute(
            select(EmailAccount).where(EmailAccount.user_id == user.id)
        )
        all_accs = list(result.scalars())
        to_del = [a for a in all_accs if (a.status or "active").strip().lower() != "active"]
        if not to_del:
            return await callback.answer("Нет неактивных аккаунтов для удаления.", show_alert=True)

        for acc in to_del:
            await session.delete(acc)
        await session.commit()
        n = len(to_del)

    await callback.answer(f"Удалено неактивных: {n}", show_alert=False)
    await render_accounts_menu(callback, callback.from_user.id, page=1, status_filter="all")


@router.callback_query(F.data == "acc_delete_all")
async def acc_delete_all_confirm(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить ВСЕ",
                    callback_data="acc_delete_all_yes",
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_accounts")],
        ]
    )
    await callback.message.edit_text(
        "⚠️ <b>Удалить все почтовые аккаунты?</b>\n\n"
        "Будут удалены все ящики из списка. Это действие нельзя отменить.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "acc_delete_all_yes")
async def acc_delete_all_yes(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        if not user:
            return await callback.answer("Пользователь не найден", show_alert=True)

        result = await session.execute(
            select(EmailAccount).where(EmailAccount.user_id == user.id)
        )
        all_accs = list(result.scalars())
        n = len(all_accs)
        for acc in all_accs:
            await session.delete(acc)
        await session.commit()

    await callback.answer(f"Удалено аккаунтов: {n}", show_alert=False)
    await render_accounts_menu(callback, callback.from_user.id, page=1, status_filter="all")


@router.callback_query(F.data == "acc_check_smtp")
async def acc_check_smtp(callback: CallbackQuery) -> None:
    tg_id = int(callback.from_user.id)
    if bg_is_running(tg_id, "accounts_smtp_check"):
        return await callback.answer("Проверка SMTP уже выполняется…", show_alert=True)

    async with Session() as session:
        user = await get_user(session, tg_id)
        if not user:
            return await callback.answer("Пользователь не найден", show_alert=True)
        accounts_n = (
            await session.execute(
                select(EmailAccount.id).where(EmailAccount.user_id == user.id)
            )
        ).all()

    if not accounts_n:
        return await callback.answer("Нет аккаунтов для проверки.", show_alert=True)

    total_accounts = len(accounts_n)
    await callback.answer("Запускаю проверку SMTP…")
    status_msg = await callback.message.answer(
        f"⏳ <b>Проверка SMTP</b>\n\n0/{total_accounts}\n"
        f"<i>Как при рассылке: SOCKS5 → Gmail, по одному ящику</i>",
        parse_mode="HTML",
    )

    async def _job() -> None:
        from services.smtp_account_check import (
            check_smtp_accounts_parallel,
            is_account_no_access_status,
            is_transient_smtp_check_failure,
        )
        from services.smtp_block_control import short_block_reason

        ok_n = 0
        blocked_n = 0
        deleted_n = 0
        skip_n = 0
        lines: List[str] = []
        total = total_accounts
        last_progress_edit = 0.0

        async def _on_progress(done: int, tot: int, email: str | None) -> None:
            nonlocal last_progress_edit
            now = asyncio.get_running_loop().time()
            if done < tot and (now - last_progress_edit) < 0.45:
                return
            last_progress_edit = now
            em = f"<code>{_e(email)}</code>\n" if email else ""
            try:
                await status_msg.edit_text(
                    f"⏳ <b>Проверка SMTP</b>\n\n{done}/{tot}\n"
                    f"<i>SOCKS5 → Gmail</i>\n{em}",
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                pass

        try:
            async with Session() as session:
                user = await get_user(session, tg_id)
                if not user:
                    await status_msg.edit_text("❌ Пользователь не найден.")
                    return
                db_uid = int(user.id)

                await session.execute(
                    update(EmailAccount)
                    .where(EmailAccount.user_id == db_uid)
                    .where(EmailAccount.status == "error")
                    .where(
                        or_(
                            EmailAccount.last_error.ilike("%starttls%"),
                            EmailAccount.last_error.ilike("%connection unexpectedly%"),
                            EmailAccount.last_error.ilike("%smtpnotsupported%"),
                            EmailAccount.last_error.ilike("%smtpserverdisconnected%"),
                        )
                    )
                    .values(status="active", last_error=None)
                )
                await session.commit()

                result = await session.execute(
                    select(EmailAccount)
                    .where(EmailAccount.user_id == db_uid)
                    .order_by(EmailAccount.id)
                )
                accounts = list(result.scalars().all())

                results = await check_smtp_accounts_parallel(
                    session,
                    db_uid,
                    accounts,
                    on_progress=_on_progress,
                )

                for res in results:
                    row = await session.get(EmailAccount, int(res.account_id))
                    if not row:
                        continue

                    st, err = res.status, res.error

                    if st is None or st == "error":
                        skip_n += 1
                        reason = short_block_reason(err) or "прокси / SMTP-туннель"
                        lines.append(
                            f"⏭ <code>{_e(res.email)}</code> — проверка не удалась\n"
                            f"   <i>{_e(reason)}</i>"
                        )
                        prev_st = (row.status or "").strip().lower()
                        if prev_st == "error" and is_transient_smtp_check_failure(err):
                            row.status = "active"
                            row.last_error = None
                            await session.commit()
                        continue

                    if is_account_no_access_status(st):
                        deleted_n += 1
                        em = row.email or ""
                        reason = short_block_reason(err)
                        await session.delete(row)
                        await session.commit()
                        lines.append(
                            f"🗑 <code>{_e(em)}</code> — удалён (нет доступа)\n"
                            f"   <i>{_e(reason)}</i>"
                        )
                        continue

                    prev = (row.status or "").strip().lower()
                    row.status = st
                    row.last_error = (err or "")[:1000] if err else None
                    await session.commit()

                    if st == "active":
                        ok_n += 1
                        if prev != "active":
                            lines.append(f"🟢 <code>{_e(res.email)}</code> — снова активен (SMTP)")
                        else:
                            lines.append(f"🟢 <code>{_e(res.email)}</code> — SMTP OK")
                    elif st == "smtp_blocked":
                        blocked_n += 1
                        reason = short_block_reason(err)
                        lines.append(
                            f"🟡 <code>{_e(res.email)}</code> — лимит/блок SMTP\n"
                            f"   <i>{_e(reason)}</i>"
                        )
                    else:
                        skip_n += 1
                        lines.append(
                            f"⏭ <code>{_e(res.email)}</code> — неизвестный сбой проверки\n"
                            f"   <i>{_e(short_block_reason(err))}</i>"
                        )

            summary = (
                "✅ <b>Проверка SMTP завершена</b>\n\n"
                f"🟢 активны (SMTP OK): <b>{ok_n}</b>\n"
                f"🟡 лимит/блок (smtp_blocked): <b>{blocked_n}</b>\n"
                f"🗑 удалено (нет доступа): <b>{deleted_n}</b>\n"
                f"⏭ проверка не удалась (ящик <u>не</u> трогали): <b>{skip_n}</b>\n\n"
                + _trim_details(lines, limit=25)
            )
            try:
                await status_msg.edit_text(summary, parse_mode="HTML")
            except TelegramBadRequest:
                await callback.message.answer(summary, parse_mode="HTML")

            await render_accounts_menu(callback, tg_id, page=1, status_filter="all")
        except Exception as e:
            logger.exception("acc_check_smtp job failed tg_id=%s", tg_id)
            err_txt = _e(str(e))[:400]
            try:
                await status_msg.edit_text(
                    f"❌ <b>Проверка SMTP упала</b>\n\n<code>{err_txt}</code>",
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                pass

    if not bg_start(tg_id, "accounts_smtp_check", _job()):
        await callback.answer("Проверка SMTP уже выполняется…", show_alert=True)


def _is_gmail_address(email: str) -> bool:
    e = (email or "").strip().lower()
    return e.endswith("@gmail.com") or e.endswith("@googlemail.com")


async def _quick_gmail_begin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AccountsQuickGmailStates.waiting_sender_name)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К списку аккаунтов", callback_data="settings_accounts")],
        ]
    )
    await message.answer(
        "⚡ <b>Быстрое добавление (Gmail)</b>\n\n"
        "<b>Шаг 1/2.</b> Введите <b>имя и фамилию</b> для отправки писем\n"
        "(например: <code>Maria Johansen</code>).\n\n"
        "Отмена: отправьте <code>-</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(F.text.in_({"⚡ Быстрое добавление", "⚡ Быстрое добавление (Gmail)"}))
async def quick_gmail_from_main_menu(message: Message, state: FSMContext) -> None:
    await _quick_gmail_begin(message, state)


@router.callback_query(F.data == "accounts_quick_gmail")
async def accounts_quick_gmail_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _quick_gmail_begin(callback.message, state)


@router.message(AccountsQuickGmailStates.waiting_sender_name)
async def quick_gmail_sender_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw == "-":
        await state.clear()
        return await message.answer("❌ Отменено.")
    words = [w for w in raw.split() if w.strip()]
    if len(words) < 2:
        return await message.answer(
            "Укажите имя и фамилию через пробел (минимум 2 слова).\n"
            "Пример: <code>Maria Johansen</code>",
            parse_mode="HTML",
        )
    await state.update_data(quick_sender_name=raw)
    await state.set_state(AccountsQuickGmailStates.waiting_gmail_creds)
    await message.answer(
        "✅ Имя сохранено.\n\n"
        "<b>Шаг 2/2.</b> Отправьте Gmail-аккаунты:\n"
        "<code>email@gmail.com:app_password</code>\n\n"
        "Каждый аккаунт — с новой строки (можно несколько).\n"
        "<b>app_password</b> — пароль приложения Google.\n\n"
        "Отмена: <code>-</code>",
        parse_mode="HTML",
    )


@router.message(AccountsQuickGmailStates.waiting_gmail_creds)
async def quick_gmail_creds(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    if raw.strip() == "-":
        await state.clear()
        return await message.answer("❌ Отменено.")

    data = await state.get_data()
    sender_name = (data.get("quick_sender_name") or "").strip()
    if not sender_name:
        await state.clear()
        return await message.answer("Сессия сброшена. Начните с «⚡ Быстрое добавление».")

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return await message.answer("Не вижу строк. Формат: <code>login@gmail.com:пароль</code>", parse_mode="HTML")

    tg_id = message.from_user.id
    if bg_is_running(tg_id, "accounts_add"):
        return await message.answer("⏳ Добавление аккаунтов уже выполняется…")

    await state.clear()
    await message.answer("⏳ Проверяю Gmail (IMAP)…")

    async def _job() -> None:
        async with Session() as session:
            user = await get_user(session, tg_id)
            if not user:
                user = User(telegram_id=tg_id)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            user.sender_name = sender_name
            ok_count, fail_count, details = await _bulk_add_accounts(
                message, session, user, lines, gmail_only=True,
            )
            await session.commit()
        summary = (
            f"⚡ <b>Готово</b>\n\n"
            f"Имя отправителя: <b>{_e(sender_name)}</b>\n"
            f"Аккаунтов добавлено: <b>{ok_count}</b>\n"
            f"Ошибок: <b>{fail_count}</b>\n\n"
            + _trim_details(details)
        )
        await message.answer(summary, parse_mode="HTML")
        await render_accounts_menu(message, tg_id, page=1, status_filter="all")

    if not bg_start(tg_id, "accounts_add", _job()):
        await message.answer("⏳ Добавление аккаунтов уже выполняется…")


@router.callback_query(F.data == "accounts_add_menu")
async def start_add_accounts(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountsAddStates.waiting_for_accounts_input)
    text = (
        "Введи список почтовых аккаунтов в формате:\n"
        "<code>email:app_password</code>\n\n"
        "Каждый аккаунт — с новой строки.\n"
        "Поддерживаются: Gmail, iCloud, GMX.\n\n"
        "<b>app_password</b> = APP PASSWORD (пароль приложения IMAP/SMTP)."
    )
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()

@router.message(AccountsAddStates.waiting_for_accounts_input)
async def process_accounts_input(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    if not lines:
        await message.answer("Я не увидел ни одной строки с аккаунтом. Попробуй ещё раз.")
        return

    tg_id = message.from_user.id
    if bg_is_running(tg_id, "accounts_add"):
        return await message.answer("⏳ Добавление аккаунтов уже выполняется…")

    await state.clear()
    await message.answer("⏳ Проверяю аккаунты (IMAP)…")

    async def _job() -> None:
        async with Session() as session:
            user = await get_user(session, tg_id)
            if not user:
                user = User(telegram_id=tg_id)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            ok_count, fail_count, details = await _bulk_add_accounts(
                message, session, user, lines, gmail_only=False,
            )
            await session.commit()
        summary = (
            f"Готово.\n\nУспешно: {ok_count}\nОшибок: {fail_count}\n\n"
            + _trim_details(details)
        )
        await message.answer(summary, parse_mode="HTML")
        await render_accounts_menu(message, tg_id, page=1, status_filter="all")

    if not bg_start(tg_id, "accounts_add", _job()):
        await message.answer("⏳ Добавление аккаунтов уже выполняется…")

@router.callback_query(F.data.startswith("acc_info:"))
async def account_info_click(callback: CallbackQuery) -> None:
    await callback.answer(
        "Это список аккаунтов. Чтобы удалить — жми на значок 🗑 справа.",
        show_alert=False,
    )

@router.callback_query(F.data.startswith("acc_del:"))
async def account_delete_click(callback: CallbackQuery) -> None:
    # поддержка старого и нового формата callback_data:
    # старый: acc_del:{id}
    # новый:  acc_del:{id}:{page}:{filter}
    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        return await callback.answer("Не понял, какой аккаунт удалить 😕", show_alert=True)

    try:
        acc_id = int(parts[1])
    except Exception:
        return await callback.answer("Не понял, какой аккаунт удалить 😕", show_alert=True)

    page = 1
    status_filter = "all"
    if len(parts) >= 4:
        try:
            page = int(parts[2])
        except Exception:
            page = 1
        status_filter = parts[3] or "all"

    async with Session() as session:
        account: Optional[EmailAccount] = await session.get(EmailAccount, acc_id)
        if not account:
            await callback.answer("Этот аккаунт уже удалён.", show_alert=True)
            return

        user = await get_user(session, callback.from_user.id)
        if not user or account.user_id != user.id:
            await callback.answer("Ты не можешь удалить чужой аккаунт.", show_alert=True)
            return

        await session.delete(account)
        await session.commit()

    await render_accounts_menu(callback, callback.from_user.id, page=page, status_filter=status_filter)
    await callback.answer("Аккаунт удалён 🗑", show_alert=False)

@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()

def _e(s: str) -> str:
    import html
    return html.escape(s or "", quote=False)
