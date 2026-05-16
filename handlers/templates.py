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

from database import Session
from models import EmailAccount
from services.users import get_or_create_user
from services.sender import send_email_via_account

router = Router()


def presets_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Показать пресеты", callback_data="presets_list")],
            [
                InlineKeyboardButton(text="➕ Добавить пресет", callback_data="tmpl_add"),
                InlineKeyboardButton(text="🗑 Удалить пресет", callback_data="tmpl_rm_menu"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить все", callback_data="tmpl_delall")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )


def presets_delete_choose_kb(items: List[TemplateItem]) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []
    for i, it in enumerate(items):
        title = it.title[:32]
        kb.append([InlineKeyboardButton(text=f"🗑 {title}", callback_data=f"tmpl_rm:{i}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="presets_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)



def smart_presets_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Показать пресеты", callback_data="smart_presets_list"),
            ],
            [
                InlineKeyboardButton(text="➕ Добавить пресет", callback_data="stmpl_add"),
                InlineKeyboardButton(text="🗑 Удалить все", callback_data="stmpl_delall"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open"),
            ],
        ]
    )

DATA_DIR = "data"
MAX_TITLE_LEN = 40
MAX_TEXT_LEN = 2000


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
        if not isinstance(x, dict):
            continue
        title = str(x.get("title", "")).strip()
        text = str(x.get("text", "")).strip()
        if title and text:
            out.append(TemplateItem(title=title, text=text))
    return out


def load_templates(tg_id: int) -> List[TemplateItem]:
    from services.user_json_store import load_json_blob_sync

    data = load_json_blob_sync(int(tg_id), "templates", default=[])
    return _items_from_json(data)


def save_templates(tg_id: int, items: List[TemplateItem]) -> None:
    from services.user_json_store import save_json_blob_sync

    data = [{"title": it.title, "text": it.text} for it in items]
    save_json_blob_sync(int(tg_id), "templates", data)


def load_smart_templates(tg_id: int) -> List[TemplateItem]:
    from services.user_json_store import load_json_blob_sync

    data = load_json_blob_sync(int(tg_id), "smart_templates", default=[])
    return _items_from_json(data)


def save_smart_templates(tg_id: int, items: List[TemplateItem]) -> None:
    from services.user_json_store import save_json_blob_sync

    data = [{"title": it.title, "text": it.text} for it in items]
    save_json_blob_sync(int(tg_id), "smart_templates", data)


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


def _render_presets(items: List[TemplateItem], header_html: str) -> str:
    """Рендер списка пресетов (для меню Пресеты/Умные пресеты) в виде как на видео."""
    if not items:
        return f"{header_html}\n\nПока пресетов нет."

    out: List[str] = [f"{header_html}\n"]
    for i, it in enumerate(items, start=1):
        title = escape(it.title)
        body = escape(it.text)
        out.append(
            f"<b>Пресет #{i}</b>\n"
            f"<u>{title}</u>\n"
            f"<blockquote>{body}</blockquote>"
        )
    return "\n\n".join(out)


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
        kb.append([InlineKeyboardButton(text=it.title[:40], callback_data=f"tmpl_send:{acc_id}:{uid}:{i}")])
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
    items = load_templates(int(user.telegram_id))
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
    items = load_templates(int(user.telegram_id))
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
# AUTO-REPLY ENGINE COMPAT
# =========================
def render_template(*, template_title: str | None = None, html_name: str | None = None, context: dict | None = None) -> str:
    """Render HTML for auto-reply.

    This function is used by services.auto_reply_engine.
    It does NOT touch UI/callbacks. It only loads an HTML file from data/html and replaces {PLACEHOLDER} tokens.

    Supported placeholders in built-in templates: {LINK}, {BUYER_NAME}, {ITEM_TITLE}, {PRICE}, {ADDRESS}.
    The values are taken from `context` by both exact key and lowercased key (e.g. LINK <- context['LINK'] or context['link']).
    Missing placeholders are replaced with empty string.
    """
    ctx = context or {}

    # Resolve html template file
    name = (html_name or "").strip() or "confirmation.html"
    p = Path("data") / "html" / name
    if not p.exists():
        # fallback to default
        p = Path("data") / "html" / "confirmation.html"
    try:
        html_text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        html_text = ""

    # Optional injection of a text-template by title (only if the HTML contains {TEMPLATE_TEXT})
    template_text = ""
    if template_title:
        try:
            tg_id = int(ctx.get("tg_id") or ctx.get("telegram_id") or 0)
            if tg_id:
                items = load_templates(tg_id)
                for it in items:
                    if (it.title or "").strip() == str(template_title).strip():
                        template_text = (it.text or "").strip()
                        break
        except Exception:
            template_text = ""

    if "{TEMPLATE_TEXT}" in html_text:
        html_text = html_text.replace("{TEMPLATE_TEXT}", template_text)

    # Replace {PLACEHOLDER}
    def repl(m):
        key = m.group(1)
        if key in ctx:
            return str(ctx.get(key) or "")
        lk = key.lower()
        if lk in ctx:
            return str(ctx.get(lk) or "")
        if key == "LINK" and "generated_link" in ctx:
            return str(ctx.get("generated_link") or "")
        return ""

    html_text = re.sub(r"\{([A-Z0-9_]+)\}", repl, html_text)
    return html_text
# =========================
# PRESETS MENU (как на видео)
# =========================

@router.callback_query(F.data == "presets_menu")
async def presets_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "🧾 <b>Пресеты</b>\n\nВыберите действие:",
        reply_markup=presets_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "presets_list")
async def presets_list(call: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = load_templates(int(user.telegram_id))
    text = _render_presets(items, "🧾 <b>Ваши пресеты:</b>")
    await _safe_edit_text(call, text=text, reply_markup=_back_only_kb("presets_menu"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "tmpl_delall")
async def presets_delete_all(call: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    save_templates(int(user.telegram_id), [])
    await presets_menu(call)


# =========================
# PRESETS ADD/DELETE (пресеты)
# =========================

class PresetAdd(StatesGroup):
    title = State()
    text = State()


_PRESETS_MENU_TEXT = "🧾 <b>Пресеты</b>\n\nВыберите действие:"
_SMART_PRESETS_MENU_TEXT = "📄 <b>Умные пресеты</b>\n\nВыберите действие:"


async def _restore_presets_menu(message: Message, state_data: dict) -> bool:
    chat_id = state_data.get("_menu_chat_id")
    msg_id = state_data.get("_menu_msg_id")
    if not chat_id or not msg_id:
        return False
    try:
        await message.bot.edit_message_text(
            _PRESETS_MENU_TEXT,
            chat_id=int(chat_id),
            message_id=int(msg_id),
            reply_markup=presets_menu_kb(),
            parse_mode="HTML",
        )
        return True
    except Exception:
        return False


async def _restore_smart_presets_menu(message: Message, state_data: dict) -> bool:
    chat_id = state_data.get("_menu_chat_id")
    msg_id = state_data.get("_menu_msg_id")
    if not chat_id or not msg_id:
        return False
    try:
        await message.bot.edit_message_text(
            _SMART_PRESETS_MENU_TEXT,
            chat_id=int(chat_id),
            message_id=int(msg_id),
            reply_markup=smart_presets_menu_kb(),
            parse_mode="HTML",
        )
        return True
    except Exception:
        return False


@router.callback_query(F.data == "tmpl_add")
async def tmpl_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PresetAdd.title)
    await state.update_data(
        _menu_chat_id=call.message.chat.id,
        _menu_msg_id=call.message.message_id,
    )
    await call.message.answer("Введите название пресета:")
    await call.answer()


@router.message(PresetAdd.title)
async def tmpl_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:MAX_TITLE_LEN]
    if not title:
        return await message.answer("Название не может быть пустым. Введите ещё раз:")
    await state.update_data(title=title)
    await state.set_state(PresetAdd.text)
    await message.answer("Отправьте текст пресета:")


@router.message(PresetAdd.text)
async def tmpl_add_text(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()[:MAX_TEXT_LEN]
    if not body:
        return await message.answer("Текст не может быть пустым. Отправьте ещё раз:")
    data = await state.get_data()
    title = str(data.get("title") or "").strip()[:MAX_TITLE_LEN]
    await state.clear()

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)

    items = load_templates(int(user.telegram_id))
    items.append(TemplateItem(title=title, text=body))
    save_templates(int(user.telegram_id), items)

    restored = await _restore_presets_menu(message, data)
    await message.answer("✅ Текст добавлен.")
    if not restored:
        await message.answer(_PRESETS_MENU_TEXT, reply_markup=presets_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "tmpl_rm_menu")
async def tmpl_rm_menu(call: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = load_templates(int(user.telegram_id))
    if not items:
        await _safe_edit_text(
            call,
            text="🧾 <b>Пресеты</b>\n\nПока пресетов нет.",
            reply_markup=_back_only_kb("presets_menu"),
            parse_mode="HTML",
        )
        return await call.answer()

    await _safe_edit_text(
        call,
        text="🧾 <b>Удаление пресета</b>\n\nВыберите пресет для удаления:",
        reply_markup=presets_delete_choose_kb(items),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("tmpl_rm:"))
async def tmpl_rm_do(call: CallbackQuery) -> None:
    try:
        idx = int(call.data.split(":", 1)[1])
    except Exception:
        return await call.answer()

    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)

    items = load_templates(int(user.telegram_id))
    if 0 <= idx < len(items):
        items.pop(idx)
        save_templates(int(user.telegram_id), items)

    await presets_menu(call)
    await call.answer()


# =========================
# SMART PRESETS (умные пресеты)
# =========================

class SmartTmplAdd(StatesGroup):
    title = State()
    text = State()


def smart_templates_manage_kb(items: List[TemplateItem], back_cb: str) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []
    for i, it in enumerate(items):
        kb.append(
            [
                InlineKeyboardButton(text=f"✏️ {it.title[:30]}", callback_data=f"stmpl_view:{i}"),
                InlineKeyboardButton(text="🗑️", callback_data=f"stmpl_del:{i}"),
            ]
        )
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def smart_templates_delete_kb(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Удалить", callback_data=f"stmpl_del_ok:{idx}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="stmpl_del_cancel"),
            ]
        ]
    )


@router.callback_query(F.data == "smart_presets_menu")
async def smart_presets_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        _SMART_PRESETS_MENU_TEXT,
        reply_markup=smart_presets_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "smart_presets_list")
async def smart_presets_list(call: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = load_smart_templates(int(user.telegram_id))
    text = _render_presets(items, "📄 <b>Ваши умные пресеты:</b>")
    await _safe_edit_text(call, text=text, reply_markup=_back_only_kb("smart_presets_menu"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "stmpl_add")
async def stmpl_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SmartTmplAdd.title)
    await state.update_data(
        _menu_chat_id=call.message.chat.id,
        _menu_msg_id=call.message.message_id,
    )
    await call.message.answer("Введите название пресета:")
    await call.answer()


@router.message(SmartTmplAdd.title)
async def stmpl_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:MAX_TITLE_LEN]
    if not title:
        return await message.answer("Название не может быть пустым. Введите ещё раз:")
    await state.update_data(title=title)
    await state.set_state(SmartTmplAdd.text)
    await message.answer("Отправьте текст пресета:")


@router.message(SmartTmplAdd.text)
async def stmpl_add_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()[:MAX_TEXT_LEN]
    if not text:
        return await message.answer("Текст не может быть пустым. Отправьте ещё раз:")
    data = await state.get_data()
    title = str(data.get("title") or "").strip()[:MAX_TITLE_LEN]
    await state.clear()

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)

    items = load_smart_templates(int(user.telegram_id))
    items.append(TemplateItem(title=title, text=text))
    save_smart_templates(int(user.telegram_id), items)

    restored = await _restore_smart_presets_menu(message, data)
    await message.answer("✅ Текст добавлен.")
    if not restored:
        await message.answer(_SMART_PRESETS_MENU_TEXT, reply_markup=smart_presets_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "stmpl_delall")
async def stmpl_delete_all(call: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    save_smart_templates(int(user.telegram_id), [])
    await smart_presets_menu(call)


@router.callback_query(F.data.startswith("stmpl_del:"))
async def stmpl_delete_ask(call: CallbackQuery) -> None:
    try:
        idx = int((call.data or "").split(":", 1)[1])
    except Exception:
        return await call.answer()
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = load_smart_templates(int(user.telegram_id))
    if idx < 0 or idx >= len(items):
        return await call.answer()
    await _safe_edit_text(call, text=f"Удалить пресет <b>{items[idx].title}</b>?", reply_markup=smart_templates_delete_kb(idx), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("stmpl_del_ok:"))
async def stmpl_delete_ok(call: CallbackQuery) -> None:
    try:
        idx = int((call.data or "").split(":", 1)[1])
    except Exception:
        return await call.answer()
    async with Session() as session:
        user = await get_or_create_user(session, call.from_user.id)
    items = load_smart_templates(int(user.telegram_id))
    if 0 <= idx < len(items):
        items.pop(idx)
        save_smart_templates(int(user.telegram_id), items)
    await smart_presets_list(call)


@router.callback_query(F.data == "stmpl_del_cancel")
async def stmpl_delete_cancel(call: CallbackQuery) -> None:
    await smart_presets_list(call)
    await call.answer()