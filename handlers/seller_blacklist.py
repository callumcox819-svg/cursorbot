"""Личный ЧС продавцов (email): не валидировать повторно на другом лоте; строгий GAG."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import Session
from models import SellerBlacklist
from services.seller_blacklist import (
    add_seller_blacklist,
    list_seller_blacklist,
    remove_seller_blacklist,
)
from services.users import get_or_create_user
from utils.callback_safe import callback_answer_safe

router = Router()


class SellerBlStates(StatesGroup):
    waiting_email = State()


def _e(s: str) -> str:
    return html.escape((s or "").strip())


def _menu_kb(rows: list[SellerBlacklist]) -> InlineKeyboardMarkup:
    lines: list[list[InlineKeyboardButton]] = []
    for r in rows[:25]:
        em = (r.seller_email or "").strip()
        lines.append(
            [
                InlineKeyboardButton(
                    text=f"❌ {em[:42]}",
                    callback_data=f"seller_bl:del:{int(r.id)}",
                )
            ]
        )
    lines.append([InlineKeyboardButton(text="➕ Добавить email", callback_data="seller_bl:add")])
    lines.append([InlineKeyboardButton(text="◀️ Назад", callback_data="seller_bl:back")])
    return InlineKeyboardMarkup(inline_keyboard=lines)


@router.callback_query(F.data == "seller_bl:menu")
async def cb_seller_bl_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback_answer_safe(callback)
    tg_id = int(callback.from_user.id)
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        rows = await list_seller_blacklist(session, int(user.id))

    text = (
        "🚫 <b>ЧС продавцов</b> (личный)\n\n"
        "Если у продавца несколько объявлений:\n"
        "• при валидации JSON — <b>не проверяем</b> его email на другом лоте;\n"
        "• при входящих / GAG — матч по <b>теме письма</b>, закрепление лота.\n\n"
        f"В списке: <b>{len(rows)}</b>\n"
        "<i>Пример: robi.rellan@gmail.com</i>"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=_menu_kb(rows),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "seller_bl:back")
async def cb_seller_bl_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback_answer_safe(callback)
    from handlers.settings import _settings_menu_kb_for_user, SETTINGS_MENU_TEXT

    kb = await _settings_menu_kb_for_user(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(SETTINGS_MENU_TEXT, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "seller_bl:add")
async def cb_seller_bl_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback_answer_safe(callback)
    await state.set_state(SellerBlStates.waiting_email)
    if callback.message:
        await callback.message.answer(
            "📧 Отправь email продавца для ЧС\n"
            "<i>Например: robi.rellan@gmail.com</i>",
            parse_mode="HTML",
        )


@router.message(SellerBlStates.waiting_email)
async def seller_bl_add_email(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw or "@" not in raw:
        await message.answer("❌ Нужен email, например <code>robi.rellan@gmail.com</code>", parse_mode="HTML")
        return
    tg_id = int(message.from_user.id)
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        ok, info = await add_seller_blacklist(session, int(user.id), raw)
        await session.commit()
        rows = await list_seller_blacklist(session, int(user.id))

    await state.clear()
    if ok:
        await message.answer(
            f"✅ Добавлен в ЧС: <code>{_e(info)}</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"⚠️ {_e(info)}", parse_mode="HTML")
    await message.answer(
        "🚫 ЧС продавцов",
        reply_markup=_menu_kb(rows),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("seller_bl:del:"))
async def cb_seller_bl_del(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback_answer_safe(callback)
    try:
        row_id = int((callback.data or "").split(":")[-1])
    except Exception:
        return
    tg_id = int(callback.from_user.id)
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        await remove_seller_blacklist(session, int(user.id), row_id)
        await session.commit()
        rows = await list_seller_blacklist(session, int(user.id))

    if callback.message:
        await callback.message.edit_text(
            "🚫 <b>ЧС продавцов</b>\n\nОбновлено.",
            reply_markup=_menu_kb(rows),
            parse_mode="HTML",
        )
