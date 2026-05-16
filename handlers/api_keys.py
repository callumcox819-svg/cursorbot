"""User settings: Goo.network keys + sender name.

ValidEmail — глобально в config.py. GAG — личный ключ пользователя (⚙️ → 🔑 Ключ), не в админке.
"""

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


# Команды для генерации ссылок (как в ТЗ)
TEAM_OPTIONS = ["AQUA", "TSUM", "NUR"]

# user_settings keys
COUNTRY_KEY = "country"
# CH / GAG profile keys (stored in user_settings)
# These constants must exist because the Profile screen reads them.
GAG_PROFILE_TITLE_KEY = "gag_profile_title"
GAG_PROFILE_NAME_KEY = "gag_profile_name"
GAG_PROFILE_ADDRESS_KEY = "gag_profile_address"
GAG_SERVICE_KEY = "gag_service"

# Optional (used by some profile screens / settings flows). Keep here for compatibility.
GAG_DOMAIN_MODE_KEY = "gag_domain_mode"  # "team" or "personal"
GAG_DOMAIN_SLOT_KEY = "gag_domain_slot"  # 1-4 when personal


def _team_key_attr(team: str | None) -> str | None:
    """User model attribute name to store Goo User API key for a specific команда."""
    if not team:
        return None
    t = team.strip().upper()
    if t == "AQUA":
        return "goo_user_api_key_aqua"
    if t == "TSUM":
        return "goo_user_api_key_tsum"
    if t == "NUR":
        return "goo_user_api_key_nur"
    return None


def _team_teamkey_attr(team: str | None) -> str | None:
    """User model attribute name to store Goo Team API key for a specific команда."""
    if not team:
        return None
    t = team.strip().upper()
    if t == "AQUA":
        return "goo_team_api_key_aqua"
    if t == "TSUM":
        return "goo_team_api_key_tsum"
    if t == "NUR":
        return "goo_team_api_key_nur"
    return None


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


def profile_screen_kb(country: str | None = None) -> InlineKeyboardMarkup:
    """Profile screen keyboard.

    UI/callback_data are fixed; for Switzerland (CH) we expose GAG profile/service
    actions inside the Profile screen (per spec).
    """
    c = (country or "").strip().upper()
    rows = []
    # For Switzerland (CH) we do NOT use Goo Profile ID at all.
    # GAG API key — глобально в config; профиль — через ➕ Создать профиль.
    if c != "CH":
        rows.append([InlineKeyboardButton(text="🛠 Установить", callback_data="goo_set:profile")])
    else:
        rows.append([InlineKeyboardButton(text="➕ Создать профиль", callback_data="gag_profile_create")])
        rows.append([InlineKeyboardButton(text="🧭 Выбор сервиса", callback_data="gag_service_menu")])
        # GAG: выбор слота домена (командный / личный 1-4)
        rows.append([InlineKeyboardButton(text="🌐 Домен", callback_data="gag_domain_menu")])
    rows.extend(
        [
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="goo_hide")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_screen_kb(*, allow_set: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if allow_set:
        rows.append([InlineKeyboardButton(text="🛠 Установить", callback_data="goo_set:user")])
    rows.extend(
        [
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="goo_hide")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teamkey_screen_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Установить", callback_data="goo_set:teamkey")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="goo_hide")],
        ]
    )


async def _render_profile_screen(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        profile_id = getattr(user, "goo_profile_id", None)
        # По ТЗ: оставляем только Швейцарию (CH) и команду GAG.
        country = "CH"
        team = "GAG"

        # For CH show GAG profile/service status instead of Goo Profile ID.
        if country == "CH":
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
                f"🧩 Команда: <b>{team or '—'}</b>\n"
            )
            try:
                await callback.message.edit_text(text, reply_markup=profile_screen_kb(country), parse_mode="HTML")
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    raise
            return

    text = (
        "🆔 <b>Текущий Profile ID</b>\n\n"
        f"<code>{profile_id or '—'}</code>\n\n"
        f"🧩 Команда: <b>{team or '—'}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=profile_screen_kb(country))


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
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"

    if country == "CH":
        async with Session() as session:
            user = await get_or_create_user(session, callback.from_user.id)
            key = await get_user_gag_api_key(session, user) or None
        status = "✅ установлен" if key else "❌ не установлен"
        text = (
            "🔑 <b>API-ключ GAG</b>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Ключ: <code>{_show_full(key)}</code>\n\n"
            "Личный ключ вашей команды (imgbeoxo). Задаётся здесь, не в админке.\n\n"
            "🧩 Команда: <b>GAG</b>"
        )
        await callback.message.edit_text(text, reply_markup=key_screen_kb(allow_set=True), parse_mode="HTML")
        return

    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        key = getattr(user, "goo_user_api_key", None)
        team = getattr(user, "goo_team_key", None)

    status = "✅ установлен" if key else "❌ не установлен"
    text = (
        "🔑 <b>Текущий API-ключ</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Ключ: <code>{_show_full(key)}</code>\n\n"
        f"🧩 Команда: <b>{team or '—'}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=key_screen_kb(allow_set=True), parse_mode="HTML")


async def _render_teamkey_screen(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        # По ТЗ: только CH/GAG. Team key не используем (оставляем экран как заглушку).
        key = None
        team = "GAG"

    status = "✅ установлен" if key else "❌ не установлен"
    text = (
        "🧷 <b>Team key (X-Team-Key)</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Ключ: <code>{_show_full(key)}</code>\n\n"
        f"🧩 Команда: <b>{team or '—'}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=teamkey_screen_kb())


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


@router.callback_query(F.data == "goo_show:teamkey")
async def goo_show_teamkey(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_teamkey_screen(callback)


def team_menu_kb(current: str | None) -> InlineKeyboardMarkup:
    rows = []
    for name in TEAM_OPTIONS:
        prefix = "✅ " if current == name else ""
        rows.append([InlineKeyboardButton(text=f"{prefix}{name}", callback_data=f"goo_team_set:{name}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gag_only_team_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ GAG", callback_data="goo_team_set:GAG")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )


@router.callback_query(F.data == "goo_team_menu")
async def goo_team_menu(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "DE").strip().upper() or "DE"
        current = getattr(user, "goo_team_key", None)

    if country == "CH":
        text = "🧩 <b>Команда</b>\n\nДля Швейцарии используется GAG."
        await callback.message.edit_text(text, reply_markup=gag_only_team_kb())
    else:
        text = "🧩 <b>Команда</b>\n\nВыбери команду для генерации ссылок:"
        await callback.message.edit_text(text, reply_markup=team_menu_kb(current))
    await callback.answer()


@router.callback_query(F.data.startswith("goo_team_set:"))
async def goo_team_set(callback: CallbackQuery) -> None:
    team = callback.data.split(":", 1)[1].strip()
    if team == "GAG":
        # Switzerland uses fixed GAG. We don't touch Goo team settings.
        await callback.answer("Команда: GAG")
        await callback.message.edit_text(
            "🧩 <b>Команда</b>\n\nДля Швейцарии используется GAG.",
            reply_markup=gag_only_team_kb(),
        )
        return

    if team not in TEAM_OPTIONS:
        await callback.answer("Неизвестная команда", show_alert=True)
        return
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        # Save team and auto-switch current key.
        user.goo_team_key = team

        # When user changes команда, we automatically swap the active key to
        # the previously saved key for that команда (if any). This prevents
        # fake "invalid credentials" on team mismatch.
        attr = _team_key_attr(team)
        if attr:
            saved = getattr(user, attr, None)
            user.goo_user_api_key = saved  # may become None => "not set"

        # Also auto-switch Team API key for the selected команда
        t_attr = _team_teamkey_attr(team)
        if t_attr:
            saved_team = getattr(user, t_attr, None)
            user.goo_team_api_key = saved_team
        await session.commit()
    await callback.answer(f"Команда: {team}")
    await callback.message.edit_text(
        "🧩 <b>Команда</b>\n\nВыбери команду для генерации ссылок:",
        reply_markup=team_menu_kb(team),
    )


@router.callback_query(F.data == "sender_name_set")
async def sender_name_set_begin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(KeysState.waiting_value)
    await state.update_data(field="sender_name")
    text = "📝 <b>Имя отправки</b>\n\nОтправь имя одним сообщением (например: <code>Support</code>)."
    await callback.message.edit_text(text, reply_markup=_back_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("goo_set:"))
async def goo_set_begin(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1].strip()
    if field not in {"profile", "user", "teamkey"}:
        await callback.answer("Неизвестное поле", show_alert=True)
        return

    await state.set_state(KeysState.waiting_value)
    await state.update_data(field=field)

    if field == "profile":
        hint = "Отправь <b>profileID</b> одним сообщением (пример: <code>T3tEktqZuli</code>)."
    elif field == "user":
        hint = "Отправь <b>User API key</b> одним сообщением."
    else:
        hint = "Отправь <b>Team API key</b> одним сообщением."

    await callback.message.edit_text("✍️ " + hint, reply_markup=_back_kb())
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
    if field in {"profile", "user", "teamkey"}:
        value = _clean_secret(value)
        if not value:
            await message.answer("❌ Пустое значение. Отправь ещё раз.")
            return

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "DE").strip().upper() or "DE"
        if field == "profile":
            user.goo_profile_id = value
        elif field == "user":
            if country == "CH":
                await set_user_setting(session, user, GAG_USER_API_KEY_KEY, value)
            else:
                # Goo.network (DE/NO): Save active key
                user.goo_user_api_key = value

                # Remember key per selected команда, so later switching команда
                # auto-swaps it.
                attr = _team_key_attr(getattr(user, "goo_team_key", None))
                if attr:
                    setattr(user, attr, value)
        elif field == "teamkey":
            # Save active Team key
            user.goo_team_api_key = value
            # Remember per selected команда
            t_attr = _team_teamkey_attr(getattr(user, "goo_team_key", None))
            if t_attr:
                setattr(user, t_attr, value)
        elif field == "sender_name":
            user.sender_name = value
        await session.commit()

    # UX: clear state and return user to the corresponding screen
    await state.clear()
    await message.answer("✅ Сохранено.")

    # Show current values immediately (like in the reference screenshots)
    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "DE").strip().upper() or "DE"
        profile_id = getattr(user, "goo_profile_id", None)
        team = "GAG" if country == "CH" else getattr(user, "goo_team_key", None)
        if country == "CH":
            key = await get_user_gag_api_key(session, user) or None
        else:
            key = getattr(user, "goo_user_api_key", None) or getattr(user, "goo_api_key", None)
        team_key = getattr(user, "goo_team_api_key", None)

    if field == "profile":
        text = (
            "🆔 <b>Текущий Profile ID</b>\n\n"
            f"<code>{profile_id or '—'}</code>\n\n"
            f"🧩 Команда: <b>{team or '—'}</b>\n"
        )
        await message.answer(text, reply_markup=profile_screen_kb(country))
    elif field == "user":
        status = "✅ установлен" if key else "❌ не установлен"
        text = (
            "🔑 <b>Текущий API-ключ</b>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Ключ: <code>{_show_full(key)}</code>\n\n"
            f"🧩 Команда: <b>{team or '—'}</b>\n"
        )
        await message.answer(text, reply_markup=key_screen_kb())
    elif field == "teamkey":
        status = "✅ установлен" if team_key else "❌ не установлен"
        text = (
            "🧷 <b>Team key (X-Team-Key)</b>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Ключ: <code>{_show_full(team_key)}</code>\n\n"
            f"🧩 Команда: <b>{team or '—'}</b>\n"
        )
        await message.answer(text, reply_markup=teamkey_screen_kb())
    else:
        # sender name or other
        await message.answer("⚙️ Настройки", reply_markup=_back_kb())
