from __future__ import annotations

import json
import os
from html import escape
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import select

from database import Session, db_session
from models import EmailAccount
from services.users import get_or_create_user
from services.sender import send_email_via_account

router = Router()


from utils.preset_list_ui import (
    NOTE_REGULAR_PRESETS,
    NOTE_SMART_PRESETS,
    REGULAR_PRESETS_EMPTY_HINT,
    named_presets_pick_kb,
    render_named_presets_page,
    render_text_presets_page,
    text_presets_manage_kb,
    text_presets_pick_kb,
)

DATA_DIR = "data"
MAX_TITLE_LEN = 40
MAX_TEXT_LEN = 4000


@dataclass
class TemplateItem:
    title: str
    text: str


# =========================
# STORAGE (Postgres на Railway / файлы локально)
# =========================
def _items_from_json(data: object) -> List[TemplateItem]:
    out: List[TemplateItem] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, str):
            text = x.strip()
            if text:
                short = text[:40] + ("…" if len(text) > 40 else "")
                out.append(TemplateItem(title=short, text=text))
            continue
        if not isinstance(x, dict):
            continue
        title = str(x.get("title", "")).strip()
        text = str(x.get("text", "")).strip()
        if not text and title:
            text = title
        if text:
            if not title:
                title = text[:40] + ("…" if len(text) > 40 else "")
            out.append(TemplateItem(title=title, text=text))
    return out


def _template_texts(items: List[TemplateItem]) -> List[str]:
    return [(it.text or "").strip() for it in items if (it.text or "").strip()]


def _template_named_pairs(items: List[TemplateItem]) -> List[tuple[str, str]]:
    return [((it.title or "").strip(), (it.text or "").strip()) for it in items if (it.text or "").strip()]


def parse_preset_name_dash_text(raw: str) -> tuple[str, str] | None:
    """Формат: «имя - текст» (дефис/тире, пробелы необязательны)."""
    s = (raw or "").strip()
    if len(s) < 4:
        return None
    m = re.match(r"^(.+?)\s*[-–—]\s*(.+)$", s, flags=re.DOTALL)
    if not m:
        return None
    name, text = m.group(1).strip(), m.group(2).strip()
    if name and len(text) >= 2:
        return name[:MAX_TITLE_LEN], text[:MAX_TEXT_LEN]
    return None


def _smart_presets_kb(has_any: bool) -> InlineKeyboardMarkup:
    return text_presets_manage_kb(
        add_cb="stmpl_add",
        edit_cb="stmpl_edit",
        del_cb="stmpl_del",
        del_all_cb="stmpl_delall",
        back_cb="settings_open",
        hide_cb="stmpl_hide",
        has_any=has_any,
    )


def _regular_presets_kb(has_any: bool) -> InlineKeyboardMarkup:
    return text_presets_manage_kb(
        add_cb="tmpl_add",
        edit_cb="tmpl_preset_edit",
        del_cb="tmpl_preset_del",
        del_all_cb="tmpl_delall",
        back_cb="settings_open",
        hide_cb="tmpl_preset_hide",
        has_any=has_any,
    )


async def load_templates(tg_id: int) -> List[TemplateItem]:
    from services.user_json_store import load_json_blob

    data = await load_json_blob(int(tg_id), "templates", default=[])
    return _items_from_json(data)


async def save_templates(tg_id: int, items: List[TemplateItem]) -> None:
    from services.user_json_store import save_json_blob

    data = [{"title": it.title, "text": it.text} for it in items]
    await save_json_blob(int(tg_id), "templates", data)


def _smart_texts_from_json(data: object) -> List[str]:
    out: List[str] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, str):
            txt = x.strip()
        elif isinstance(x, dict):
            txt = str(x.get("text", "")).strip() or str(x.get("title", "")).strip()
        else:
            txt = str(x).strip()
        if txt:
            out.append(txt[:MAX_TEXT_LEN])
    return out


async def load_smart_texts(tg_id: int) -> List[str]:
    from services.user_json_store import load_json_blob

    data = await load_json_blob(int(tg_id), "smart_templates", default=[])
    return _smart_texts_from_json(data)


async def save_smart_texts(tg_id: int, texts: List[str]) -> None:
    from services.user_json_store import save_json_blob

    clean = [t.strip()[:MAX_TEXT_LEN] for t in texts if (t or "").strip()]
    await save_json_blob(int(tg_id), "smart_templates", clean)


async def _mailing_text_pool(tg_id: int) -> List[str]:
    """Только 📄 Умные пресеты (рассылка /send и тест-почта). 🧾 Пресеты — для ответа на письмо."""
    return list(await load_smart_texts(int(tg_id)))


async def pick_random_smart_preset(tg_id: int, offer_title: str) -> str:
    """Случайный умный пресет: спинтакс {a|b} → OFFER = название товара."""
    import random

    from services.offer_text import apply_offer_to_text
    from services.spintax import expand_spintax

    texts = await _mailing_text_pool(tg_id)
    if not texts:
        return ""
    base = texts[random.randrange(len(texts))]
    txt = expand_spintax(base)
    return apply_offer_to_text(txt, offer_title)


def _load_templates_sync(tg_id: int) -> List[TemplateItem]:
    """Sync read from local JSON only (render_template / non-async callers)."""
    from services.user_json_store import _load_from_filesystem

    data = _load_from_filesystem(int(tg_id), "templates", default=[])
    return _items_from_json(data)


# =========================
# KEYBOARDS
# =========================
def templates_manage_kb(items: List[TemplateItem], back_cb: str = "settings_open") -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []
    for i, it in enumerate(items):
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"✏️ {it.title[:30]}",
                    callback_data=f"tmpl_edit:{i}",
                ),
                InlineKeyboardButton(
                    text="🗑️",
                    callback_data=f"tmpl_del:{i}",
                ),
            ]
        )
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def templates_delete_kb(idx: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="✅ Удалить", callback_data=f"tmpl_del_ok:{idx}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="tmpl_del_cancel"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# =========================
# FSM
# =========================
class TmplAdd(StatesGroup):
    title = State()
    text = State()


def _render_manage(items: List[TemplateItem]) -> str:
    if not items:
        return "⚡️ <b>Шаблоны</b>\n\nПока шаблонов нет."
    lines = ["⚡️ <b>Шаблоны</b>\n"]
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. <b>{it.title}</b>")
    return "\n".join(lines)


async def _safe_edit_text(
    call: CallbackQuery,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    **kwargs,
) -> None:
    """Безопасно редактирует текст сообщения.

    Игнорирует ошибку Telegram "message is not modified".
    Принимает любые kwargs (в т.ч. parse_mode) и пробрасывает их в edit_text.
    """
    if "parse_mode" not in kwargs:
        kwargs["parse_mode"] = "HTML"
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

def _back_only_kb(back_cb: str) -> InlineKeyboardMarkup:
    """Клавиатура только с кнопкой Назад."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )

def _quick_templates_kb(items: List[TemplateItem], acc_id: int, uid: str) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []
    for i, it in enumerate(items):
        label = (it.text or it.title or "")[:40]
        kb.append([InlineKeyboardButton(text=label, callback_data=f"tmpl_send:{acc_id}:{uid}:{i}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tmpl_close:{acc_id}:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def _norm_re_subject(subject: str) -> str:
    s = (subject or "").strip()
    return re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", s, flags=re.I).strip()


@router.callback_query(F.data.startswith("tmpl_open:"))
async def tmpl_open_for_mail(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        await call.answer()
        return
    acc_id = int(parts[1])
    uid = parts[2]

    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = await load_templates(int(user.telegram_id))
    if not items:
        await call.answer("Шаблонов нет", show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=_quick_templates_kb(items, acc_id, uid))
    await call.answer()


@router.callback_query(F.data.startswith("tmpl_close:"))
async def tmpl_close_for_mail(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.startswith("tmpl_send:"))
async def tmpl_send_for_mail(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 4:
        await call.answer()
        return
    acc_id = int(parts[1])
    uid = parts[2]
    idx = int(parts[3])

    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = await load_templates(int(user.telegram_id))
    if idx < 0 or idx >= len(items):
        await call.answer("Шаблон не найден", show_alert=True)
        return

    item = items[idx]

    async with Session() as session:
        acc = (await session.execute(select(EmailAccount).where(EmailAccount.id == acc_id))).scalars().first()

    if not acc:
        await call.answer("Аккаунт не найден", show_alert=True)
        return

    # Здесь отправка шаблона по твоей существующей логике (не трогаем UI)
    # Реальные адреса/тема берутся из handlers/incoming_mail по callback, поэтому тут оставлено как было.
    await call.answer("Шаблон выбран", show_alert=False)


# =========================
# PRESETS / SMART PRESETS (единый экран как на скрине)
# =========================

class PresetAdd(StatesGroup):
    name = State()
    text = State()


class PresetEdit(StatesGroup):
    idx = State()
    name = State()
    text = State()


class SmartTmplAdd(StatesGroup):
    text = State()


class SmartTmplEdit(StatesGroup):
    idx = State()
    text = State()


async def _user_tg_id(session, from_user_id: int) -> int:
    user = await get_or_create_user(session, from_user_id)
    return int(user.telegram_id)


async def _edit_menu_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> bool:
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        return False
    except Exception:
        return False


async def _restore_presets_list(message: Message, state_data: dict, tg_id: int) -> bool:
    chat_id = state_data.get("_menu_chat_id")
    msg_id = state_data.get("_menu_msg_id")
    if not chat_id or not msg_id:
        return False
    items = await load_templates(tg_id)
    texts = _template_texts(items)
    return await _edit_menu_message(
        message.bot,
        chat_id=int(chat_id),
        message_id=int(msg_id),
        text=render_text_presets_page("🧾 <b>Ваши пресеты:</b>", texts, footer_note=NOTE_REGULAR_PRESETS),
        reply_markup=_regular_presets_kb(bool(texts)),
    )


async def _restore_smart_list(message: Message, state_data: dict, tg_id: int) -> bool:
    chat_id = state_data.get("_menu_chat_id")
    msg_id = state_data.get("_menu_msg_id")
    if not chat_id or not msg_id:
        return False
    texts = await load_smart_texts(tg_id)
    return await _edit_menu_message(
        message.bot,
        chat_id=int(chat_id),
        message_id=int(msg_id),
        text=render_text_presets_page(
            "📄 <b>Ваши умные пресеты:</b>",
            texts,
            footer_note=NOTE_SMART_PRESETS,
        ),
        reply_markup=_smart_presets_kb(bool(texts)),
    )


async def _delete_message_safe(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _hide_old_menu_markup(bot, state_data: dict) -> None:
    chat_id = state_data.get("_menu_chat_id")
    msg_id = state_data.get("_menu_msg_id")
    if not chat_id or not msg_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=int(chat_id), message_id=int(msg_id), reply_markup=None)
    except Exception:
        pass


async def _send_presets_menu_message(message: Message, tg_id: int) -> None:
    items = await load_templates(tg_id)
    pairs = _template_named_pairs(items)
    await message.answer(
        render_named_presets_page(
            "🧾 <b>Ваши пресеты:</b>",
            pairs,
            empty_hint=REGULAR_PRESETS_EMPTY_HINT,
            footer_note=NOTE_REGULAR_PRESETS,
        ),
        reply_markup=_regular_presets_kb(bool(pairs)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _send_smart_menu_message(message: Message, tg_id: int) -> None:
    texts = await load_smart_texts(tg_id)
    await message.answer(
        render_text_presets_page(
            "📄 <b>Ваши умные пресеты:</b>",
            texts,
            footer_note=NOTE_SMART_PRESETS,
        ),
        reply_markup=_smart_presets_kb(bool(texts)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _finish_presets_add(message: Message, state_data: dict, tg_id: int) -> None:
    prompt_id = state_data.get("_prompt_msg_id")
    if prompt_id:
        await _delete_message_safe(message.bot, message.chat.id, int(prompt_id))
    await _hide_old_menu_markup(message.bot, state_data)
    await message.answer("✅ Добавлено.")
    await _send_presets_menu_message(message, tg_id)


async def _finish_smart_add(message: Message, state_data: dict, tg_id: int) -> None:
    prompt_id = state_data.get("_prompt_msg_id")
    if prompt_id:
        await _delete_message_safe(message.bot, message.chat.id, int(prompt_id))
    await _hide_old_menu_markup(message.bot, state_data)
    await message.answer("✅ Добавлено.")
    await _send_smart_menu_message(message, tg_id)


@router.callback_query(F.data == "presets_menu")
async def presets_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        _menu_chat_id=call.message.chat.id,
        _menu_msg_id=call.message.message_id,
    )
    async with db_session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_templates(tg_id)
    pairs = _template_named_pairs(items)
    await call.message.edit_text(
        render_named_presets_page(
            "🧾 <b>Ваши пресеты:</b>",
            pairs,
            empty_hint=REGULAR_PRESETS_EMPTY_HINT,
            footer_note=NOTE_REGULAR_PRESETS,
        ),
        reply_markup=_regular_presets_kb(bool(pairs)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "tmpl_delall")
async def presets_delete_all(call: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    await save_templates(tg_id, [])
    await call.answer("Удалено")
    await presets_menu(call, state)


@router.callback_query(F.data == "tmpl_preset_hide")
async def tmpl_preset_hide(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Скрыто")


@router.callback_query(F.data == "tmpl_add")
async def tmpl_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        _menu_chat_id=call.message.chat.id,
        _menu_msg_id=call.message.message_id,
    )
    await state.set_state(PresetAdd.name)
    prompt = await call.message.answer(
        "➕ <b>Шаг 1/2.</b> Отправь <b>имя пресета</b> — оно будет на кнопке при ответе на письмо.\n\n"
        "Пример: <code>новый пресет</code>",
        parse_mode="HTML",
    )
    await state.update_data(_prompt_msg_id=prompt.message_id)
    await call.answer()


@router.message(PresetAdd.name)
async def tmpl_add_name(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:MAX_TITLE_LEN]
    if len(title) < 1:
        return await message.answer("Имя не может быть пустым. Введи ещё раз.")
    # Одной строкой «имя - текст» тоже можно (короткие пресеты)
    parsed = parse_preset_name_dash_text(message.text or "")
    if parsed:
        title, body = parsed
        data = await state.get_data()
        await state.clear()
        async with Session() as session:
            tg_id = await _user_tg_id(session, message.from_user.id)
        items = await load_templates(tg_id)
        items.append(TemplateItem(title=title, text=body))
        await save_templates(tg_id, items)
        await _finish_presets_add(message, data, tg_id)
        return
    await state.update_data(preset_name=title)
    await state.set_state(PresetAdd.text)
    await message.answer(
        "➕ <b>Шаг 2/2.</b> Отправь <b>текст пресета</b> — его получит адресат письма.\n"
        "Можно длинное сообщение целиком одним текстом.",
        parse_mode="HTML",
    )


@router.message(PresetAdd.text)
async def tmpl_add_text(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()[:MAX_TEXT_LEN]
    if len(body) < 2:
        return await message.answer("Текст слишком короткий. Отправь ещё раз.")
    data = await state.get_data()
    title = str(data.get("preset_name") or "").strip()[:MAX_TITLE_LEN]
    if not title:
        await state.clear()
        return await message.answer("Имя пресета потеряно. Нажми «➕ Добавить пресет» снова.")
    await state.clear()

    async with Session() as session:
        tg_id = await _user_tg_id(session, message.from_user.id)
    items = await load_templates(tg_id)
    items.append(TemplateItem(title=title, text=body))
    await save_templates(tg_id, items)
    await _finish_presets_add(message, data, tg_id)


@router.callback_query(F.data == "tmpl_preset_del")
async def tmpl_preset_del_pick(call: CallbackQuery) -> None:
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_templates(tg_id)
    if not items:
        return await call.answer("Пусто")
    pairs = _template_named_pairs(items)
    await call.message.edit_text(
        "🗑 Выбери пресет для удаления:",
        reply_markup=named_presets_pick_kb(pairs, "tmpl_preset_del", "presets_menu"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("tmpl_preset_del:"))
async def tmpl_preset_del_idx(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[1])
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_templates(tg_id)
    if idx < 0 or idx >= len(items):
        return await call.answer("Не найден", show_alert=True)
    items.pop(idx)
    await save_templates(tg_id, items)
    await call.answer("Удалено")
    await presets_menu(call, state)


@router.callback_query(F.data == "tmpl_preset_edit")
async def tmpl_preset_edit_pick(call: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_templates(tg_id)
    if not items:
        return await call.answer("Пусто")
    await state.update_data(_menu_chat_id=call.message.chat.id, _menu_msg_id=call.message.message_id)
    await state.set_state(PresetEdit.idx)
    pairs = _template_named_pairs(items)
    await call.message.edit_text(
        "✏️ Выбери пресет для изменения:",
        reply_markup=named_presets_pick_kb(pairs, "tmpl_preset_edit", "presets_menu"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("tmpl_preset_edit:"))
async def tmpl_preset_edit_choose(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[1])
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_templates(tg_id)
    if idx < 0 or idx >= len(items):
        return await call.answer("Не найден", show_alert=True)
    await state.update_data(idx=idx)
    await state.set_state(PresetEdit.name)
    old = items[idx]
    await call.message.answer(
        f"✏️ <b>Шаг 1/2.</b> Новое имя пресета (сейчас: <code>{escape(old.title)}</code>):",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(PresetEdit.name)
async def tmpl_preset_edit_name(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:MAX_TITLE_LEN]
    if len(title) < 1:
        return await message.answer("Имя не может быть пустым.")
    await state.update_data(preset_name=title)
    await state.set_state(PresetEdit.text)
    await message.answer(
        "✏️ <b>Шаг 2/2.</b> Отправь новый текст письма для этого пресета:",
        parse_mode="HTML",
    )


@router.message(PresetEdit.text)
async def tmpl_preset_edit_text(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()[:MAX_TEXT_LEN]
    if len(body) < 2:
        return await message.answer("Текст слишком короткий. Введи ещё раз.")
    data = await state.get_data()
    idx = int(data.get("idx", -1))
    title = str(data.get("preset_name") or "").strip()[:MAX_TITLE_LEN]
    if not title:
        await state.clear()
        return await message.answer("Имя не задано. Начни изменение заново.")
    await state.clear()

    async with Session() as session:
        tg_id = await _user_tg_id(session, message.from_user.id)
    items = await load_templates(tg_id)
    if idx < 0 or idx >= len(items):
        return await message.answer("Пресет не найден.")
    items[idx] = TemplateItem(title=title, text=body)
    await save_templates(tg_id, items)
    await _hide_old_menu_markup(message.bot, data)
    await message.answer("✅ Сохранено.")
    await _send_presets_menu_message(message, tg_id)


@router.callback_query(F.data == "smart_presets_menu")
async def smart_presets_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        _menu_chat_id=call.message.chat.id,
        _menu_msg_id=call.message.message_id,
    )
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    texts = await load_smart_texts(tg_id)
    await call.message.edit_text(
        render_text_presets_page(
            "📄 <b>Ваши умные пресеты:</b>",
            texts,
            footer_note=NOTE_SMART_PRESETS,
        ),
        reply_markup=_smart_presets_kb(bool(texts)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await call.answer()


@router.callback_query(F.data == "stmpl_hide")
async def stmpl_hide(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Скрыто")


@router.callback_query(F.data == "stmpl_add")
async def stmpl_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        _menu_chat_id=call.message.chat.id,
        _menu_msg_id=call.message.message_id,
    )
    await state.set_state(SmartTmplAdd.text)
    prompt = await call.message.answer(
        "➕ Отправь текст пресета одним сообщением.\n"
        "Можно <code>OFFER</code> и спинтаксис <code>{a|b|c}</code>.",
        parse_mode="HTML",
    )
    await state.update_data(_prompt_msg_id=prompt.message_id)
    await call.answer()


@router.message(SmartTmplAdd.text)
async def stmpl_add_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()[:MAX_TEXT_LEN]
    if len(text) < 2:
        return await message.answer("Текст слишком короткий. Отправь ещё раз.")
    data = await state.get_data()
    await state.clear()

    async with Session() as session:
        tg_id = await _user_tg_id(session, message.from_user.id)
    items = await load_smart_texts(tg_id)
    items.append(text)
    await save_smart_texts(tg_id, items)
    await _finish_smart_add(message, data, tg_id)


@router.callback_query(F.data == "stmpl_delall")
async def stmpl_delete_all(call: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    await save_smart_texts(tg_id, [])
    await call.answer("Удалено")
    await smart_presets_menu(call, state)


@router.callback_query(F.data == "stmpl_del")
async def stmpl_del_pick(call: CallbackQuery) -> None:
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_smart_texts(tg_id)
    if not items:
        return await call.answer("Пусто")
    await call.message.edit_text(
        "🗑 Выбери пресет для удаления:",
        reply_markup=text_presets_pick_kb(len(items), "stmpl_del", "smart_presets_menu"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("stmpl_del:"))
async def stmpl_del_idx(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[1])
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_smart_texts(tg_id)
    if idx < 0 or idx >= len(items):
        return await call.answer("Не найден", show_alert=True)
    items.pop(idx)
    await save_smart_texts(tg_id, items)
    await call.answer("Удалено")
    await smart_presets_menu(call, state)


@router.callback_query(F.data == "stmpl_edit")
async def stmpl_edit_pick(call: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_smart_texts(tg_id)
    if not items:
        return await call.answer("Пусто")
    await state.update_data(_menu_chat_id=call.message.chat.id, _menu_msg_id=call.message.message_id)
    await state.set_state(SmartTmplEdit.idx)
    await call.message.edit_text(
        "✏️ Выбери пресет для изменения:",
        reply_markup=text_presets_pick_kb(len(items), "stmpl_edit", "smart_presets_menu"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("stmpl_edit:"))
async def stmpl_edit_choose(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[1])
    async with Session() as session:
        tg_id = await _user_tg_id(session, call.from_user.id)
    items = await load_smart_texts(tg_id)
    if idx < 0 or idx >= len(items):
        return await call.answer("Не найден", show_alert=True)
    await state.update_data(idx=idx)
    await state.set_state(SmartTmplEdit.text)
    await call.message.answer("✏️ Отправь новый текст пресета одним сообщением.")
    await call.answer()


@router.message(SmartTmplEdit.text)
async def stmpl_edit_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()[:MAX_TEXT_LEN]
    if len(text) < 2:
        return await message.answer("Текст слишком короткий. Введи ещё раз.")
    data = await state.get_data()
    idx = int(data.get("idx", -1))
    await state.clear()

    async with Session() as session:
        tg_id = await _user_tg_id(session, message.from_user.id)
    items = await load_smart_texts(tg_id)
    if idx < 0 or idx >= len(items):
        return await message.answer("Пресет не найден.")
    items[idx] = text
    await save_smart_texts(tg_id, items)
    await _hide_old_menu_markup(message.bot, data)
    await message.answer("✅ Сохранено.")
    await _send_smart_menu_message(message, tg_id)