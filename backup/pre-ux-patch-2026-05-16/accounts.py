from __future__ import annotations

import logging
import re
import imaplib
from typing import List, Optional, Tuple

from aiogram import Router, F
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

# ===========================
# Helpers
# ===========================

IMAP_SERVERS: dict[str, Tuple[str, str]] = {
    "gmail.com": ("imap.gmail.com", "gmail"),
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

    ok_count = 0
    fail_count = 0
    details: List[str] = []

    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            user = User(telegram_id=message.from_user.id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        for line in lines:
            if ":" not in line:
                fail_count += 1
                details.append(f"❌ <code>{_e(line)}</code> — нет разделителя <code>:</code>")
                continue

            email, password = line.split(":", 1)
            email = email.strip()
            password = password.strip()

            if not email or not password:
                fail_count += 1
                details.append(f"❌ <code>{_e(line)}</code> — пустой email или пароль")
                continue

            ok, provider, err = check_imap_credentials(email, password)
            if not ok:
                fail_count += 1
                details.append(f"❌ <code>{_e(email)}</code> — {_e(err or 'ошибка при входе')}")
                continue

            existing_res = await session.execute(
                select(EmailAccount).where(
                    EmailAccount.user_id == user.id,
                    EmailAccount.email == email,
                )
            )
            existing = existing_res.scalar_one_or_none()
            if existing:
                existing.password = password
                existing.provider = provider
                existing.status = "active"
            else:
                acc = EmailAccount(
                    user_id=user.id,
                    email=email,
                    password=password,
                    provider=provider,
                    status="active",
                )
                session.add(acc)

            ok_count += 1
            details.append(f"✅ <code>{_e(email)}</code> — добавлен ({_e(provider or '')})")

        await session.commit()

    summary = f"Готово.\n\nУспешно: {ok_count}\nОшибок: {fail_count}\n\n" + "\n".join(details)
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
