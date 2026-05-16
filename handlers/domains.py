from __future__ import annotations

import re
from typing import Dict, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy import select, delete

from database import Session
from models import Domain
from services.users import get_or_create_user

router = Router()

from aiogram.exceptions import TelegramBadRequest


async def _safe_edit_text(message, text: str, reply_markup=None, **kwargs):
    """Edit message text but ignore Telegram "message is not modified" errors.

    Accepts extra aiogram kwargs (e.g. parse_mode) to avoid TypeError.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        raise


"""Домены.

Сделано максимально "чисто" для чата:
- меню доменов всегда inline (клики не создают сообщений);
- ввод домена остаётся обычным сообщением (его удалить нельзя в личке),
  но ответы бота не плодятся — мы редактируем одно меню.
"""

# pending на пользователя:
#  action: "add" | "remove"
#  menu_message_id: id сообщения-меню, которое мы редактируем
_domain_pending: Dict[int, dict] = {}


# ====== КЛАВИАТУРЫ ======


def domains_menu_kb() -> InlineKeyboardMarkup:
    """Красивое меню доменов: inline, чтобы клики не засоряли чат."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data="domains_add"),
                InlineKeyboardButton(text="➖ Удалить", callback_data="domains_remove"),
            ],
            [InlineKeyboardButton(text="📄 Список доменов", callback_data="domains_list")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="domains_back")],
        ]
    )


# ====== УТИЛИТЫ ======

_domain_re = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.IGNORECASE)


def _normalize_domain(text: str) -> Optional[str]:
    if not text:
        return None

    t = text.strip().lower()

    # если прислали email — вытащим домен
    if "@" in t and t.count("@") == 1:
        t = t.split("@", 1)[1]

    t = t.replace("http://", "").replace("https://", "").strip("/")
    if "/" in t:
        t = t.split("/", 1)[0]

    if not _domain_re.match(t):
        return None

    return t


async def _list_domains_text(user_id: int) -> str:
    async with Session() as session:
        rows = (
            await session.execute(
                select(Domain.domain).where(Domain.user_id == user_id)
            )
        ).all()

    domains = [r[0] for r in rows]

    if not domains:
        return "📭 Домены не добавлены.\n\nНажми «➕ Добавить»."

    return "🌐 Твои домены:\n\n" + "\n".join(f"• {d}" for d in sorted(domains))


# ====== МЕНЮ ======

@router.message(F.text.in_({"🌐 Домены", "Домены", "⚙️ Домены"}))
async def domains_open(message: Message):
    # Открытие из текстовой кнопки (если кто-то всё же её использует)
    # Не плодим сообщения: создаём одно меню и дальше его редактируем.
    _domain_pending.pop(message.from_user.id, None)
    menu = await message.answer(
        "🌐 <b>Управление доменами</b>\n\nВыбери действие:",
        reply_markup=domains_menu_kb(),
    )
    _domain_pending[message.from_user.id] = {
        "action": None,
        "menu_message_id": menu.message_id,
        "chat_id": message.chat.id,
    }


@router.callback_query(F.data == "domains_list")
async def cb_domains_list(callback: CallbackQuery):
    _domain_pending[callback.from_user.id] = {
        "action": None,
        "menu_message_id": callback.message.message_id,
        "chat_id": callback.message.chat.id,
    }
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        uid = user.id
    await _safe_edit_text(callback.message,
        await _list_domains_text(uid),
        reply_markup=domains_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "domains_add")
async def cb_domains_add_begin(callback: CallbackQuery):
    _domain_pending[callback.from_user.id] = {
        "action": "add",
        "menu_message_id": callback.message.message_id,
        "chat_id": callback.message.chat.id,
    }
    await _safe_edit_text(callback.message, 
        "✍️ Отправь домен одним сообщением\nНапример: <code>example.com</code>",
        reply_markup=domains_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "domains_remove")
async def cb_domains_remove_begin(callback: CallbackQuery):
    _domain_pending[callback.from_user.id] = {
        "action": "remove",
        "menu_message_id": callback.message.message_id,
        "chat_id": callback.message.chat.id,
    }
    await _safe_edit_text(callback.message, 
        "✍️ Отправь домен для удаления\nНапример: <code>example.com</code>",
        reply_markup=domains_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "domains_back")
async def cb_domains_back(callback: CallbackQuery):
    _domain_pending.pop(callback.from_user.id, None)

    # вернёмся в меню настроек
    from keyboards.settings_menu import settings_menu

    await _safe_edit_text(
        callback.message,
        "⚙️ <b>Настройки</b>\n\nВыберите раздел:",
        reply_markup=settings_menu(),
        parse_mode="HTML",
    )
    await callback.answer()


"""Раньше здесь был ReplyKeyboardMarkup и команды текстом.
Оставили текстовый ввод только для самого домена, а меню сделали inline,
чтобы в чате не появлялись сообщения вида "➕ Добавить домен".
"""


# ====== ВВОД ДОМЕНА ======

def _domain_input_pending(message: Message) -> bool:
    ctx = _domain_pending.get(message.from_user.id)
    return bool(ctx and ctx.get("action"))


@router.message(F.func(_domain_input_pending))
async def domains_text_input(message: Message):
    ctx = _domain_pending.get(message.from_user.id)
    action = (ctx or {}).get("action")

    domain = _normalize_domain(message.text)
    if not domain:
        # Не шлём новых сообщений — просто обновим меню с ошибкой
        await message.bot.edit_message_text(
            chat_id=ctx["chat_id"],
            message_id=ctx["menu_message_id"],
            text=(
                "❌ Это не домен. Пример: <code>example.com</code>\n\n"
                "Попробуй ещё раз: отправь домен одним сообщением."
            ),
            reply_markup=domains_menu_kb(),
        )
        return

    tg_id = message.from_user.id
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        user_id = user.id

    if action == "add":
        async with Session() as session:
            exists = (
                await session.execute(
                    select(Domain.id).where(
                        Domain.user_id == user_id,
                        Domain.domain == domain,
                    )
                )
            ).first()

            if exists:
                await message.bot.edit_message_text(
                    chat_id=ctx["chat_id"],
                    message_id=ctx["menu_message_id"],
                    text=(
                        f"ℹ️ Домен <b>{domain}</b> уже есть.\n\n"
                        "Можешь добавить другой или посмотреть список."
                    ),
                    reply_markup=domains_menu_kb(),
                )
                ctx["action"] = None
                return

            session.add(Domain(user_id=user_id, domain=domain))
            await session.commit()

        await message.bot.edit_message_text(
            chat_id=ctx["chat_id"],
            message_id=ctx["menu_message_id"],
            text=(f"✅ Добавил: <b>{domain}</b>\n\n" + await _list_domains_text(user_id)),
            reply_markup=domains_menu_kb(),
        )
        ctx["action"] = None
        return

    if action == "remove":
        async with Session() as session:
            await session.execute(
                delete(Domain).where(
                    Domain.user_id == user_id,
                    Domain.domain == domain,
                )
            )
            await session.commit()

        await message.bot.edit_message_text(
            chat_id=ctx["chat_id"],
            message_id=ctx["menu_message_id"],
            text=(f"🗑 Удалил: <b>{domain}</b>\n\n" + await _list_domains_text(user_id)),
            reply_markup=domains_menu_kb(),
        )
        ctx["action"] = None
        return
