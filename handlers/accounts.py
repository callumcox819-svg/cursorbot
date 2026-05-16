from __future__ import annotations

import asyncio
import logging
import re
import imaplib
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
from sqlalchemy import select

from database import Session
from models import User, EmailAccount
from keyboards.main_menu import main_menu_kb
from keyboards.settings_menu import settings_menu

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
) -> None:
    try:
        await status_msg.edit_text(
            "⏳ <b>Добавление аккаунтов</b>\n\n"
            f"Проверка IMAP: <b>{current}/{total}</b>\n"
            f"✅ успешно: <b>{ok}</b> · ❌ ошибки: <b>{fail}</b>",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass


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
    status_msg = await message.answer(
        f"⏳ <b>Добавление аккаунтов</b>\n\nПроверка IMAP: <b>0/{total}</b>",
        parse_mode="HTML",
    )

    ok_count = 0
    fail_count = 0
    details: List[str] = []

    for i, line in enumerate(lines, start=1):
        if ":" not in line:
            fail_count += 1
            if gmail_only:
                details.append(f"❌ <code>{_e(line)}</code> — нет <code>:</code>")
            else:
                details.append(f"❌ <code>{_e(line)}</code> — нет разделителя <code>:</code>")
            await _edit_add_progress(status_msg, current=i, total=total, ok=ok_count, fail=fail_count)
            continue

        email, password = line.split(":", 1)
        email = email.strip()
        password = password.strip()

        if gmail_only and not _is_gmail_address(email):
            fail_count += 1
            details.append(f"❌ <code>{_e(email)}</code> — только @gmail.com / @googlemail.com")
            await _edit_add_progress(status_msg, current=i, total=total, ok=ok_count, fail=fail_count)
            continue

        if not email or not password:
            fail_count += 1
            details.append(f"❌ <code>{_e(line)}</code> — пустой email или пароль")
            await _edit_add_progress(status_msg, current=i, total=total, ok=ok_count, fail=fail_count)
            continue

        ok, provider, err = await _imap_check_async(email, password)
        if not ok:
            fail_count += 1
            err_txt = _e(err or ("ошибка IMAP" if gmail_only else "ошибка при входе"))
            details.append(f"❌ <code>{_e(email)}</code> — {err_txt}")
            await _edit_add_progress(status_msg, current=i, total=total, ok=ok_count, fail=fail_count)
            continue

        existing_res = await session.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == user.id,
                EmailAccount.email == email,
            )
        )
        existing = existing_res.scalar_one_or_none()
        prov = provider or "gmail"
        if existing:
            existing.password = password
            existing.provider = prov
            existing.status = "active"
        else:
            session.add(
                EmailAccount(
                    user_id=user.id,
                    email=email,
                    password=password,
                    provider=prov,
                    status="active",
                )
            )

        ok_count += 1
        if gmail_only:
            details.append(f"✅ <code>{_e(email)}</code>")
        else:
            details.append(f"✅ <code>{_e(email)}</code> — добавлен ({_e(prov)})")
        await _edit_add_progress(status_msg, current=i, total=total, ok=ok_count, fail=fail_count)

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
        emoji = "🟢" if acc.status == "active" else "🔴"
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

    # Действия (как на скрине)
    rows.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="accounts_add_menu")])
    rows.append([InlineKeyboardButton(text="📥 Массовое добавление", callback_data="accounts_add_menu")])
    rows.append([InlineKeyboardButton(text="⚡ Быстрое добавление (Gmail)", callback_data="accounts_quick_gmail")])

    # ✅ ТЗ: имя отправителя задаётся отдельной кнопкой в меню E-mail.
    # Используем уже существующий экран sender_name_menu (handlers/settings.py).
    rows.append([InlineKeyboardButton(text="📝 Задать имя отправителя", callback_data="sender_name_menu")])

    # Фильтр
    filt_title = "🔎 Фильтр по статусу"
    rows.append([InlineKeyboardButton(text=filt_title, callback_data=f"acc_filter:{status_filter}:{page}")])

    # (Опционально) Массовое удаление — пока безопасный “заглушечный” пункт, чтобы не ломать
    rows.append([InlineKeyboardButton(text="🧹 Удалить аккаунты", callback_data="acc_bulk_delete_stub")])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

async def render_accounts_menu(message_or_cb, telegram_id: int, page: int = 1, status_filter: str = "all") -> None:
    async with Session() as session:
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
            "Нажми «➕ Добавить аккаунт» и введи:\n"
            "<code>email:app_password</code>\n\n"
            "Поддерживаются: Gmail, iCloud, GMX.\n"
            "Используй APP PASSWORD (пароль приложения)."
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
    await callback.answer()

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

@router.callback_query(F.data == "acc_bulk_delete_stub")
async def acc_bulk_delete_stub(callback: CallbackQuery) -> None:
    # Заглушка, чтобы не ломать — массовое удаление добавим отдельным безопасным шагом.
    await callback.answer("Массовое удаление добавим отдельно (безопасно). Пока удаляй 🗑 справа.", show_alert=True)


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


# Reply «⚡ Быстрое добавление» — bot._bind_main_reply_keyboard

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

    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            user = User(telegram_id=message.from_user.id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        user.sender_name = sender_name
        ok_count, fail_count, details = await _bulk_add_accounts(
            message, session, user, lines, gmail_only=True,
        )
        await session.commit()

    await state.clear()

    summary = (
        f"⚡ <b>Готово</b>\n\n"
        f"Имя отправителя: <b>{_e(sender_name)}</b>\n"
        f"Аккаунтов добавлено: <b>{ok_count}</b>\n"
        f"Ошибок: <b>{fail_count}</b>\n\n"
        + _trim_details(details)
    )
    await message.answer(summary, parse_mode="HTML")
    await render_accounts_menu(message, message.from_user.id, page=1, status_filter="all")


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

    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            user = User(telegram_id=message.from_user.id)
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

    await state.clear()
    await render_accounts_menu(message, message.from_user.id, page=1, status_filter="all")

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
