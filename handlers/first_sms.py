from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from utils.preset_list_ui import NOTE_SMART_PRESETS, render_text_presets_page, text_presets_manage_kb, text_presets_pick_kb


router = Router()

DATA_DIR = "data"


@dataclass
class FirstSmsPreset:
    text: str


async def load_presets(tg_id: int) -> List[FirstSmsPreset]:
    from services.user_json_store import load_json_blob

    data = await load_json_blob(int(tg_id), "first_sms", default=[])
    out: List[FirstSmsPreset] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, dict):
            txt = str(x.get("text", "")).strip()
        else:
            txt = str(x).strip()
        if txt:
            out.append(FirstSmsPreset(text=txt))
    return out


async def save_presets(tg_id: int, items: List[FirstSmsPreset]) -> None:
    from services.user_json_store import save_json_blob

    await save_json_blob(int(tg_id), "first_sms", [{"text": t.text} for t in items])


async def pick_random_first_sms(tg_id: int, offer_title: str) -> str:
    """Pick random preset, apply spintax and OFFER placeholder."""
    from services.spintax import expand_spintax

    items = await load_presets(tg_id)
    base = items[random.randrange(len(items))].text if items else "Hello! Is this item still available? OFFER"
    txt = expand_spintax(base)
    title = (offer_title or "").strip()
    if title:
        txt = txt.replace("OFFER", title)
    return txt


def _manage_kb(has_any: bool) -> InlineKeyboardMarkup:
    return text_presets_manage_kb(
        add_cb="fsms_add",
        edit_cb="fsms_edit",
        del_cb="fsms_del",
        del_all_cb="fsms_del_all",
        back_cb="settings_open",
        hide_cb="fsms_hide",
        has_any=has_any,
    )


def _pick_kb(count: int, action: str) -> InlineKeyboardMarkup:
    return text_presets_pick_kb(count, f"fsms_{action}", "firstsms_open")


def _render_list(items: List[FirstSmsPreset]) -> str:
    texts = [p.text for p in items]
    return render_text_presets_page(
        "📄 <b>Ваши умные пресеты:</b>",
        texts,
        footer_note=NOTE_SMART_PRESETS,
    )


class FsAdd(StatesGroup):
    text = State()


class FsEdit(StatesGroup):
    idx = State()
    text = State()


@router.callback_query(F.data == "firstsms_open")
async def firstsms_open(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    items = await load_presets(callback.from_user.id)
    await callback.message.edit_text(
        _render_list(items),
        reply_markup=_manage_kb(bool(items)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "fsms_hide")
async def fsms_hide(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Скрыто")


@router.callback_query(F.data == "fsms_add")
async def fsms_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FsAdd.text)
    # Remember the menu message so we can return there after the user sends the text.
    # This keeps the flow clean: add preset -> send text -> immediately see the preset list again.
    await state.update_data(_back_chat_id=callback.message.chat.id, _back_msg_id=callback.message.message_id)
    await callback.message.answer(
        "➕ Отправь текст пресета одним сообщением.\n"
        "Можно использовать <code>OFFER</code> и спинтаксис <code>{a|b|c}</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FsAdd.text)
async def fsms_add_text(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if len(txt) < 2:
        await message.answer("Текст слишком короткий. Введи ещё раз.")
        return
    items = await load_presets(message.from_user.id)
    items.append(FirstSmsPreset(text=txt))
    await save_presets(message.from_user.id, items)

    # ✅ Важно: пресет добавляем ТОЛЬКО после нажатия кнопки "➕ Добавить пресет".
    # Поэтому после успешного добавления очищаем state, чтобы любые дальнейшие
    # сообщения пользователя не улетали автоматически в "первые смс".
    data = await state.get_data()
    await state.clear()

    back_chat_id = data.get("_back_chat_id")
    back_msg_id = data.get("_back_msg_id")

    # Try to edit the original menu message; if it no longer exists, send a fresh menu message.
    try:
        if back_chat_id and back_msg_id:
            await message.bot.edit_message_text(
                _render_list(items),
                chat_id=int(back_chat_id),
                message_id=int(back_msg_id),
                reply_markup=_manage_kb(True),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return  # меню обновлено, без лишнего спама
    except Exception:
        pass

    # Fallback: just show the menu as a new message.
    await message.answer(
        _render_list(items),
        reply_markup=_manage_kb(True),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await message.answer("✅ Пресет добавлен.")


@router.callback_query(F.data == "fsms_del_all")
async def fsms_del_all(callback: CallbackQuery, state: FSMContext):
    await save_presets(callback.from_user.id, [])
    await callback.answer("Удалено")
    # ✅ firstsms_open требует FSMContext, иначе падаем TypeError
    await firstsms_open(callback, state)


@router.callback_query(F.data == "fsms_del")
async def fsms_del_pick(callback: CallbackQuery):
    items = await load_presets(callback.from_user.id)
    if not items:
        await callback.answer("Пусто")
        return
    await callback.message.edit_text("🗑 Выбери пресет для удаления:", reply_markup=_pick_kb(len(items), "del"))
    await callback.answer()


@router.callback_query(F.data.startswith("fsms_del:"))
async def fsms_del_idx(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    items = await load_presets(callback.from_user.id)
    if idx < 0 or idx >= len(items):
        await callback.answer("Не найден", show_alert=True)
        return
    items.pop(idx)
    await save_presets(callback.from_user.id, items)
    await callback.answer("Удалено")
    await firstsms_open(callback, state)


@router.callback_query(F.data == "fsms_edit")
async def fsms_edit_pick(callback: CallbackQuery, state: FSMContext):
    items = await load_presets(callback.from_user.id)
    if not items:
        await callback.answer("Пусто")
        return
    # Remember where to return after editing.
    await state.update_data(_back_chat_id=callback.message.chat.id, _back_msg_id=callback.message.message_id)
    await state.set_state(FsEdit.idx)
    await callback.message.edit_text("✏️ Выбери пресет для изменения:", reply_markup=_pick_kb(len(items), "edit"))
    await callback.answer()


@router.callback_query(F.data.startswith("fsms_edit:"))
async def fsms_edit_choose(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    items = await load_presets(callback.from_user.id)
    if idx < 0 or idx >= len(items):
        await callback.answer("Не найден", show_alert=True)
        return
    await state.update_data(idx=idx)
    await state.set_state(FsEdit.text)
    await callback.message.answer("✏️ Отправь новый текст пресета одним сообщением.")
    await callback.answer()


@router.message(FsEdit.text)
async def fsms_edit_text(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if len(txt) < 2:
        await message.answer("Текст слишком короткий. Введи ещё раз.")
        return
    data = await state.get_data()
    idx = int(data.get("idx", -1))
    back_chat_id = data.get("_back_chat_id")
    back_msg_id = data.get("_back_msg_id")
    items = await load_presets(message.from_user.id)
    if idx < 0 or idx >= len(items):
        await message.answer("Пресет не найден.")
        await state.clear()
        return
    items[idx] = FirstSmsPreset(text=txt)
    await save_presets(message.from_user.id, items)
    await state.clear()

    # Return to the menu.
    try:
        if back_chat_id and back_msg_id:
            await message.bot.edit_message_text(
                _render_list(items),
                chat_id=int(back_chat_id),
                message_id=int(back_msg_id),
                reply_markup=_manage_kb(True),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
    except Exception:
        pass

    await message.answer(_render_list(items), reply_markup=_manage_kb(True), parse_mode="HTML", disable_web_page_preview=True)
    await message.answer("✅ Пресет обновлён.")
