from __future__ import annotations

import os
import sys

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from config import config
from database import Session
from services.users import get_or_create_user
from services.team_keys import TEAM_OPTIONS, set_team_api_key, get_team_api_key
from sqlalchemy import select, func

from models import EmailAccount, SentEmail, OfferEmail, Offer, User


router = Router(name="admin_panel")


async def is_admin(tg_id: int) -> bool:
    if int(tg_id) in set(getattr(config, "ADMIN_IDS", []) or []):
        return True
    async with Session() as session:
        user = await get_or_create_user(session, int(tg_id))
        return bool(getattr(user, "is_admin", False))


class AdminState(StatesGroup):
    waiting_teamkey_value = State()
    waiting_grant_admin = State()
    waiting_revoke_admin = State()
    waiting_allow = State()
    waiting_deny = State()
    waiting_stats = State()


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика пользователей", callback_data="admin_user_stats")],
            [InlineKeyboardButton(text="✅ Выдать доступ", callback_data="admin_allow")],
            [InlineKeyboardButton(text="⛔ Удалить доступ", callback_data="admin_deny")],
            [InlineKeyboardButton(text="🔑 Ключ тимы", callback_data="admin_teamkey")],
            [InlineKeyboardButton(text="👑 Выдать админ права", callback_data="admin_grant_admin")],
            [InlineKeyboardButton(text="🔄 Рестарт", callback_data="admin_restart")],
        ]
    )


@router.message(F.text.in_({"/admin", "👑 Админ-панель", "🔥 Админ-панель"}))
async def open_admin(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к админ-панели.")
        return
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text("👑 <b>Админ-панель</b>", reply_markup=admin_kb())
    except TelegramBadRequest as e:
        # Telegram ругается, если мы пытаемся "перерисовать" то же самое сообщение
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "admin_allow")
async def admin_allow_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_allow)
    await callback.message.edit_text(
        "✅ <b>Выдать доступ</b>\n\nОтправь Telegram ID пользователя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]]),
    )
    await callback.answer()


@router.message(AdminState.waiting_allow)
async def admin_allow_finish(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        tid = int((message.text or "").strip())
    except Exception:
        await message.answer("❌ Это не число. Отправь Telegram ID.")
        return
    async with Session() as session:
        u = await get_or_create_user(session, tid)
        u.is_banned = False
        u.access_granted = True
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Доступ выдан: <code>{tid}</code>")
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin_deny")
async def admin_deny_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_deny)
    await callback.message.edit_text(
        "⛔ <b>Удалить доступ</b>\n\nОтправь Telegram ID пользователя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]]),
    )
    await callback.answer()


@router.message(AdminState.waiting_deny)
async def admin_deny_finish(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        tid = int((message.text or "").strip())
    except Exception:
        await message.answer("❌ Это не число. Отправь Telegram ID.")
        return
    async with Session() as session:
        u = await get_or_create_user(session, tid)
        u.access_granted = False
        u.is_banned = False
        await session.commit()
    await state.clear()
    await message.answer(f"⛔ Доступ удалён: <code>{tid}</code>")
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_kb())

@router.callback_query(F.data == "admin_user_stats")
async def admin_stats_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    async with Session() as session:
        # active = all known users in DB
        rows = (await session.execute(select(User.telegram_id).order_by(User.created_at.desc()))).scalars().all()
    ids = [str(x) for x in rows[:30]]
    text = "📊 <b>Статистика пользователей</b>\n\n"
    text += f"Всего пользователей в БД: <b>{len(rows)}</b>\n\n"
    if ids:
        text += "Последние активные Telegram ID:\n" + "\n".join(f"• <code>{i}</code>" for i in ids)
        if len(rows) > 30:
            text += f"\n… и ещё {len(rows)-30}"
    else:
        text += "Пока нет пользователей в БД."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Проверить", callback_data="admin_user_stats_check")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_user_stats_check")
async def admin_stats_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_stats)
    await callback.message.edit_text(
        "📊 <b>Статистика пользователя</b>\n\nОтправь Telegram ID пользователя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_user_stats")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminState.waiting_stats)
async def admin_stats_finish(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        tid = int((message.text or "").strip())
    except Exception:
        await message.answer("❌ Это не число. Отправь Telegram ID.")
        return

    async with Session() as session:
        u = await get_or_create_user(session, tid)
        # accounts
        total_accounts = (await session.execute(select(func.count()).select_from(EmailAccount).where(EmailAccount.user_id == u.id))).scalar() or 0
        # sent emails
        sent_count = (await session.execute(select(func.count()).select_from(SentEmail).where(SentEmail.user_id == u.id))).scalar() or 0
        # validated emails = count offer_emails for user's offers
        validated = (await session.execute(
            select(func.count()).select_from(OfferEmail).join(Offer, Offer.id == OfferEmail.offer_id).where(Offer.user_id == u.id)
        )).scalar() or 0

    await state.clear()
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Telegram ID: <code>{tid}</code>\n"
        f"Доступ: <b>{'✅ есть' if getattr(u, 'access_granted', False) and not getattr(u, 'is_banned', False) else '⛔ нет'}</b>\n\n"
        f"📮 Аккаунтов: <b>{total_accounts}</b>\n"
        f"📧 Валидных email: <b>{validated}</b>\n"
        f"✉️ Отправлено (антидубль): <b>{sent_count}</b>",
    )
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin_teamkey")
async def admin_teamkey(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f"admin_teamkey:{t}") for t in TEAM_OPTIONS],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")],
        ]
    )
    await callback.message.edit_text("🔑 <b>Ключ тимы</b>\n\nВыбери команду:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_teamkey:"))
async def admin_teamkey_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    team = callback.data.split(":", 1)[1].strip().upper()
    async with Session() as session:
        key = await get_team_api_key(session, team)
    status = "✅ установлен" if key else "❌ не установлен"
    text = (
        f"🔑 <b>Ключ тимы</b> — <b>{team}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Ключ: <code>{key or '—'}</code>\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Установить", callback_data=f"admin_teamkey_set:{team}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_teamkey")],
        ]
    )
    await state.clear()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_teamkey_set:"))
async def admin_teamkey_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    team = callback.data.split(":", 1)[1].strip().upper()
    await state.update_data(team=team)
    await state.set_state(AdminState.waiting_teamkey_value)
    await callback.message.edit_text(
        f"🔑 <b>Установить ключ тимы</b> — <b>{team}</b>\n\nОтправь Team API Key одним сообщением.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_teamkey:{team}")]]
        ),
    )
    await callback.answer()


@router.message(AdminState.waiting_teamkey_value)
async def admin_teamkey_finish(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    team = (data.get("team") or "").strip().upper()
    key = (message.text or "").strip()
    async with Session() as session:
        await set_team_api_key(session, team, key if key else None)
    await state.clear()
    await message.answer("✅ Сохранено (глобально для всех пользователей).")
    # reopen screen
    fake_cb = type("obj", (), {"from_user": message.from_user, "data": f"admin_teamkey:{team}", "message": message, "answer": lambda *a, **k: None})
    # Use normal flow:
    await open_admin(message)


@router.callback_query(F.data == "admin_grant_admin")
async def admin_admins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    async with Session() as session:
        rows = (await session.execute(select(User.telegram_id).where(User.is_admin == True).order_by(User.created_at.desc()))).scalars().all()
    ids = [str(x) for x in rows[:30]]
    text = "👑 <b>Админ-права</b>\n\n"
    text += f"Админов сейчас: <b>{len(rows)}</b>\n\n"
    if ids:
        text += "Активные админы (Telegram ID):\n" + "\n".join(f"• <code>{i}</code>" for i in ids)
        if len(rows) > 30:
            text += f"\n… и ещё {len(rows)-30}"
    else:
        text += "Админов в БД пока нет (кроме тех, кто в config.ADMIN_IDS)."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Выдать", callback_data="admin_admin_grant_begin")],
            [InlineKeyboardButton(text="➖ Забрать", callback_data="admin_admin_revoke_begin")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_admin_grant_begin")
async def admin_grant_admin_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_grant_admin)
    await callback.message.edit_text(
        "➕ <b>Выдать админ права</b>\n\nОтправь Telegram ID пользователя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_grant_admin")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_admin_revoke_begin")
async def admin_revoke_admin_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_revoke_admin)
    await callback.message.edit_text(
        "➖ <b>Забрать админ права</b>\n\nОтправь Telegram ID пользователя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_grant_admin")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminState.waiting_grant_admin)
async def admin_grant_admin_finish(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        tid = int((message.text or "").strip())
    except Exception:
        await message.answer("❌ Неверный ID. Отправь число.")
        return
    async with Session() as session:
        user = await get_or_create_user(session, tid)
        user.is_admin = True
        user.access_granted = True
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Админ права выданы пользователю <code>{tid}</code>.")
    await open_admin(message)


@router.callback_query(F.data == "admin_restart")
async def admin_restart(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Рестарт…")
    # Restart current python process
    os.execv(sys.executable, [sys.executable] + sys.argv)
