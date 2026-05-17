"""GAG API key + profile (Швейцария). ValidEmail — в config.py."""

from __future__ import annotations

import re

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from database import Session
from config import config
from services.users import get_or_create_user
from services.gag_keys import GAG_USER_API_KEY as GAG_USER_API_KEY_KEY, get_user_gag_api_key
from services.user_settings import get_user_setting, set_user_setting
from utils.secrets import clean_secret


async def _is_admin(tg_id: int) -> bool:
    if int(tg_id) in set(getattr(config, "ADMIN_IDS", []) or []):
        return True
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        return bool(getattr(user, "is_admin", False))


router = Router(name="api_keys")


# user_settings keys
# CH / GAG profile keys (stored in user_settings)
# These constants must exist because the Profile screen reads them.
GAG_PROFILE_TITLE_KEY = "gag_profile_title"
GAG_PROFILE_NAME_KEY = "gag_profile_name"
GAG_PROFILE_ADDRESS_KEY = "gag_profile_address"
GAG_SERVICE_KEY = "gag_service"

# Optional (used by some profile screens / settings flows). Keep here for compatibility.
GAG_DOMAIN_MODE_KEY = "gag_domain_mode"  # "team" or "personal"
GAG_DOMAIN_SLOT_KEY = "gag_domain_slot"  # 1-4 when personal


class KeysState(StatesGroup):
    waiting_value = State()


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")]]
    )


def _hide_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🟢 Скрыть", callback_data="goo_hide")]]
    )


def _clean_secret(value: str | None) -> str:
    """Backward-compatible wrapper (used in several handlers)."""
    return clean_secret(value)

def _show_full(key: str | None) -> str:
    return (key or "—").strip() or "—"


def profile_screen_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать профиль", callback_data="gag_profile_create")],
            [InlineKeyboardButton(text="🧭 Выбор сервиса", callback_data="gag_service_menu")],
            [InlineKeyboardButton(text="🌐 Домен", callback_data="gag_domain_menu")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="goo_hide")],
        ]
    )


def key_screen_kb(*, allow_set: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if allow_set:
        rows.append([InlineKeyboardButton(text="🛠 Установить", callback_data="gag_set:key")])
    rows.extend(
        [
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="goo_hide")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_profile_screen(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        prof_title = (await get_user_setting(session, user, GAG_PROFILE_TITLE_KEY) or "").strip() or "—"
        prof_name = (await get_user_setting(session, user, GAG_PROFILE_NAME_KEY) or "").strip() or "—"
        prof_addr = (await get_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY) or "").strip() or "—"
        service = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip() or "—"
        domain_mode = (await get_user_setting(session, user, GAG_DOMAIN_MODE_KEY) or "team").strip() or "team"
        domain_slot = int(await get_user_setting(session, user, GAG_DOMAIN_SLOT_KEY) or 1)
        domain_text = "Домен команды" if domain_mode != "personal" else f"Домен {domain_slot}"
        text = (
            "👤 <b>Профиль GAG</b>\n\n"
            f"Название профиля: <code>{prof_title}</code>\n"
            f"Имя покупателя: <code>{prof_name}</code>\n"
            f"Адрес: <code>{prof_addr}</code>\n"
            f"Сервис: <b>{service}</b>\n\n"
            f"🌐 Домен: <b>{domain_text}</b>\n\n"
            "🧩 Команда: <b>GAG</b> · 🇨🇭 Швейцария\n"
        )
    try:
        await callback.message.edit_text(text, reply_markup=profile_screen_kb(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def gag_domain_menu_kb(mode: str, slot: int) -> InlineKeyboardMarkup:
    mode = (mode or "team").strip() or "team"
    slot = int(slot or 1)

    def mark_team() -> str:
        return "✅ " if mode != "personal" else ""

    def mark_personal(n: int) -> str:
        return "✅ " if mode == "personal" and slot == n else ""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{mark_team()}🏢 Домен команды", callback_data="gag_domain_set:team")],
            [InlineKeyboardButton(text=f"{mark_personal(1)}⬜ Домен 1", callback_data="gag_domain_set:1")],
            [InlineKeyboardButton(text=f"{mark_personal(2)}⬜ Домен 2", callback_data="gag_domain_set:2")],
            [InlineKeyboardButton(text=f"{mark_personal(3)}⬜ Домен 3", callback_data="gag_domain_set:3")],
            [InlineKeyboardButton(text=f"{mark_personal(4)}⬜ Домен 4", callback_data="gag_domain_set:4")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="goo_show:profile")],
        ]
    )


@router.callback_query(F.data == "gag_domain_menu")
async def gag_domain_menu(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        mode = (await get_user_setting(session, user, GAG_DOMAIN_MODE_KEY) or "team").strip() or "team"
        slot = int(await get_user_setting(session, user, GAG_DOMAIN_SLOT_KEY) or 1)

    text = (
        "🌐 <b>Домен GAG</b>\n\n"
        "Выбери режим домена.\n"
        "— <b>Домен команды</b>: стандартный домен (по умолчанию)\n"
        "— <b>Домен 1–4</b>: личные домены (в API идут как 5–8: 1→5, 2→6, 3→7, 4→8)"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=gag_domain_menu_kb(mode, slot),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data.startswith("gag_domain_set:"))
async def gag_domain_set(callback: CallbackQuery) -> None:
    raw = (callback.data or "").split(":", 1)[1]
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        if raw == "team":
            await set_user_setting(session, user, GAG_DOMAIN_MODE_KEY, "team")
            # slot оставляем как есть (на будущее), но режим = team
        else:
            try:
                slot = int(raw)
            except Exception:
                slot = 1
            slot = max(1, min(4, slot))
            await set_user_setting(session, user, GAG_DOMAIN_MODE_KEY, "personal")
            await set_user_setting(session, user, GAG_DOMAIN_SLOT_KEY, str(slot))

        mode = (await get_user_setting(session, user, GAG_DOMAIN_MODE_KEY) or "team").strip() or "team"
        slot_now = int(await get_user_setting(session, user, GAG_DOMAIN_SLOT_KEY) or 1)

    # просто обновляем меню, без каких-либо API вызовов
    try:
        await callback.message.edit_reply_markup(reply_markup=gag_domain_menu_kb(mode, slot_now))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise



async def _render_key_screen(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        key = await get_user_gag_api_key(session, user) or None
    status = "✅ установлен" if key else "❌ не установлен"
    text = (
        "🔑 <b>API-ключ GAG</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Ключ: <code>{_show_full(key)}</code>\n\n"
        "Личный ключ команды GAG (imgbeoxo).\n\n"
        "🧩 Команда: <b>GAG</b> · 🇨🇭 Швейцария"
    )
    await callback.message.edit_text(text, reply_markup=key_screen_kb(allow_set=True), parse_mode="HTML")


@router.callback_query(F.data == "goo_hide")
async def goo_hide(callback: CallbackQuery) -> None:
    # Удаляем чувствительные данные из экрана (оставляем пустую заглушку)
    await callback.message.edit_text("✅ Скрыто.")
    await callback.answer()


@router.callback_query(F.data.in_({"goo_show:profile", "gag_show:profile"}))
async def goo_show_profile(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_profile_screen(callback)


@router.callback_query(F.data.in_({"goo_show:key", "gag_show:key"}))
async def goo_show_key(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_key_screen(callback)


@router.callback_query(F.data == "sender_name_set")
async def sender_name_set_begin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(KeysState.waiting_value)
    await state.update_data(field="sender_name")
    text = "📝 <b>Имя отправки</b>\n\nОтправь имя одним сообщением (например: <code>Support</code>)."
    await callback.message.edit_text(text, reply_markup=_back_kb())
    await callback.answer()


@router.callback_query(F.data == "gag_set:key")
async def gag_set_key_begin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(KeysState.waiting_value)
    await state.update_data(field="gag_key")
    await callback.message.edit_text(
        "✍️ Отправь <b>API-ключ GAG</b> одним сообщением.",
        reply_markup=_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(KeysState.waiting_value)
async def keys_set_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("field")
    raw = (message.text or "")
    value = raw.strip()
    if not value:
        await message.answer("❌ Пустое значение. Отправь ещё раз.")
        return
    if field == "gag_key":
        value = _clean_secret(value)
        if not value:
            await message.answer("❌ Пустое значение. Отправь ещё раз.")
            return

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        if field == "gag_key":
            await set_user_setting(session, user, GAG_USER_API_KEY_KEY, value)
        elif field == "sender_name":
            user.sender_name = value
        await session.commit()

    await state.clear()
    await message.answer("✅ Сохранено.")

    if field == "gag_key":
        async with Session() as session:
            user = await get_or_create_user(session, message.from_user.id)
            key = await get_user_gag_api_key(session, user) or None
        status = "✅ установлен" if key else "❌ не установлен"
        text = (
            "🔑 <b>API-ключ GAG</b>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Ключ: <code>{_show_full(key)}</code>\n\n"
            "🧩 Команда: <b>GAG</b>"
        )
        await message.answer(text, reply_markup=key_screen_kb())
    else:
        await message.answer("⚙️ Настройки", reply_markup=_back_kb())
